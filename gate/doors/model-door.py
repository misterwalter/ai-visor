#!/usr/bin/env python3
"""The model door: the one way from inside the wall to the model.

Listens on a unix socket and passes chat requests on to the model server.
Everything else is refused and logged. The model server's API can also pull,
push, create and delete models, and a pull is an outbound request to a host of
the caller's choosing, so none of that may be reachable from inside the wall.

Usage: model-door.py <socket path> <upstream host:port>
"""

import http.client
import http.server
import os
import socketserver
import sys

ALLOWED_PATH = "/v1/chat/completions"
# Headers that describe one connection, not the message, and must not be passed on.
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "te", "upgrade"}


def log(text):
    sys.stderr.write(f"model-door: {text}\n")
    sys.stderr.flush()


class Door(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = None  # (host, port), set in main

    def log_message(self, format, *args):
        pass  # the default logger wants a client address, and a unix socket has none

    def refuse(self):
        log(f"REFUSED {self.command} {self.path}")
        body = f"the wall allows only POST {ALLOWED_PATH}\n".encode()
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse

    def do_POST(self):
        if self.path != ALLOWED_PATH:
            return self.refuse()
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self.refuse()
        body = self.rfile.read(int(length))

        upstream = http.client.HTTPConnection(*self.upstream, timeout=None)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS | {"host"}}
            upstream.request("POST", ALLOWED_PATH, body, headers)
            reply = upstream.getresponse()
            self.send_response(reply.status)
            for key, value in reply.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            # Pass the reply on as it arrives; the harness reads it as a stream.
            while True:
                chunk = reply.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            log(f"passed POST {ALLOWED_PATH} -> {reply.status}")
        except (BrokenPipeError, ConnectionResetError):
            log("the harness closed the connection before the reply ended")
        except OSError as error:
            log(f"FAILED to reach the model server at {self.upstream}: {error}")
            if not self.wfile.closed:
                try:
                    self.send_error(502, "model server unreachable")
                except OSError:
                    pass
        finally:
            upstream.close()  # also tells the model server to stop generating
            self.close_connection = True


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    path, upstream = sys.argv[1], sys.argv[2]
    host, _, port = upstream.rpartition(":")
    if not host or not port.isdigit():
        sys.exit(f"model-door: upstream must be host:port, got {upstream!r}")
    Door.upstream = (host, int(port))
    if os.path.exists(path):
        os.unlink(path)
    with Server(path, Door) as server:
        log(f"open at {path}, passing to {upstream}")
        server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The net door: the one way from inside the wall to the internet, and it leads
only into the VPN tunnel.

Listens on a unix socket and answers as a web proxy: CONNECT for https, and
plain http requests. Every connection it makes is to one address, the tunnel's
SOCKS proxy on this machine, which is handed the destination by name. The door
looks no name up and connects to nothing else. So nothing inside the wall can
reach the internet except through the tunnel, whatever it runs and however it
is set up, and with the tunnel down every request fails here.

A request for this machine or a private network is refused. The model server is
on this machine, and the model door is the only way to it.

Every destination is logged out here, where the agent cannot alter the record.

Usage: net-door.py <socket path> <tunnel proxy host:port>
"""

import http.server
import ipaddress
import os
import select
import socket
import socketserver
import struct
import sys
import urllib.parse

# Headers that describe one connection, not the message, and must not be passed on.
HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade"}
# How long the tunnel has to reach a destination.
CONNECT_SECONDS = 60
# What a SOCKS proxy says when it cannot make a connection.
SOCKS_ERRORS = {1: "general failure", 2: "not allowed", 3: "network unreachable", 4: "host unreachable",
                5: "connection refused", 6: "timed out", 7: "command not supported", 8: "address type not supported"}


def log(text):
    sys.stderr.write(f"net-door: {text}\n")
    sys.stderr.flush()


class TunnelError(Exception):
    """Why the tunnel made no connection."""


def is_local(host):
    """Whether a destination is this machine or a private network, by its name or its number."""
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def receive(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise TunnelError("the tunnel's proxy closed the connection")
        data += chunk
    return data


def through_tunnel(proxy, host, port):
    """A connection to host:port, made by the tunnel's SOCKS proxy. A name goes to the
    proxy as it is: looking it up here would send the question outside the tunnel."""
    try:
        address = ipaddress.ip_address(host)
        destination = (b"\x01" if address.version == 4 else b"\x04") + address.packed
    except ValueError:
        try:
            name = host.encode("idna")
            destination = b"\x03" + bytes([len(name)]) + name
        except ValueError:  # an encoding error is one too
            raise TunnelError(f"{host!r} is not a name the tunnel can be asked for") from None
    try:
        sock = socket.create_connection(proxy, timeout=CONNECT_SECONDS)
    except OSError as error:
        raise TunnelError(f"the VPN tunnel is not running at {proxy[0]}:{proxy[1]} ({error})") from None
    try:
        sock.sendall(b"\x05\x01\x00")  # SOCKS 5, offering one way to sign in: none
        if receive(sock, 2) != b"\x05\x00":
            raise TunnelError("the tunnel's proxy wants a password")
        sock.sendall(b"\x05\x01\x00" + destination + struct.pack(">H", port))
        _, reply, _, kind = receive(sock, 4)
        if reply != 0:
            raise TunnelError(f"the tunnel could not reach it ({SOCKS_ERRORS.get(reply, f'error {reply}')})")
        # The proxy's own end of the connection follows; nothing here needs it.
        receive(sock, {1: 4, 4: 16}[kind] if kind in (1, 4) else receive(sock, 1)[0])
        receive(sock, 2)
        sock.settimeout(None)
        return sock
    except OSError as error:
        sock.close()
        raise TunnelError(f"the tunnel did not answer ({error})") from None
    except TunnelError:
        sock.close()
        raise


def relay(one, other):
    """Pass bytes both ways until either side closes."""
    try:
        while True:
            ready, _, _ = select.select([one, other], [], [])
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (other if source is one else one).sendall(data)
    except OSError:
        return


class Door(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Nothing is read ahead: after CONNECT, what follows is the destination's, not the door's.
    rbufsize = 0
    proxy = None  # (host, port) of the tunnel's SOCKS proxy, set in main

    def log_message(self, format, *args):
        pass  # the default logger wants a client address, and a unix socket has none

    def answer(self, status, text):
        body = f"the net door: {text}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def refuse(self, reason):
        log(f"REFUSED {self.command} {self.path}: {reason}")
        self.answer(403, reason)

    def reach(self, host, port):
        """The connection to a destination, through the tunnel. None once the caller has
        been told why there is none."""
        if is_local(host):
            self.refuse("this machine and private networks are not reached through this door")
            return None
        try:
            upstream = through_tunnel(self.proxy, host, port)
        except TunnelError as error:
            log(f"FAILED {self.command} {host}:{port}: {error}")
            self.answer(502, str(error))
            return None
        log(f"passed {self.command} {host}:{port}")
        return upstream

    def do_CONNECT(self):
        host, _, port = self.path.rpartition(":")
        host = host.strip("[]")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            return self.refuse("CONNECT needs host:port")
        upstream = self.reach(host, int(port))
        if upstream is None:
            return
        with upstream:
            self.send_response(200, "Connection established")
            self.end_headers()
            relay(self.connection, upstream)
        self.close_connection = True

    def forward(self):
        """A plain http request, asked for by its full address as a proxy is asked."""
        url = urllib.parse.urlsplit(self.path)
        try:
            host, port = url.hostname, url.port or 80
        except ValueError:
            host = None
        if url.scheme != "http" or not host:
            return self.refuse("this is a proxy: ask for a full http:// address, or CONNECT for https")
        length = self.headers.get("Content-Length", "0")
        if "Transfer-Encoding" in self.headers or not length.isdigit():
            return self.refuse("a request with a body needs a Content-Length")
        body = b""
        while len(body) < int(length):
            chunk = self.rfile.read(int(length) - len(body))
            if not chunk:
                return self.refuse("the request's body ended early")
            body += chunk
        upstream = self.reach(host, port)
        if upstream is None:
            return
        # One request to a connection, so that every destination passes through reach().
        head = [f"{self.command} {urllib.parse.urlunsplit(('', '', url.path or '/', url.query, ''))} HTTP/1.1"]
        head += [f"{key}: {value}" for key, value in self.headers.items() if key.lower() not in HOP_HEADERS]
        head += ["Connection: close", "", ""]
        with upstream:
            upstream.sendall("\r\n".join(head).encode("latin-1") + body)
            relay(self.connection, upstream)
        self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = forward


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    path, proxy = sys.argv[1:3]
    host, _, port = proxy.rpartition(":")
    if not host or not port.isdigit():
        sys.exit(f"net-door: the tunnel's proxy must be host:port, got {proxy!r}")
    Door.proxy = (host, int(port))
    if os.path.exists(path):
        os.unlink(path)
    with Server(path, Door) as server:
        log(f"open at {path}, leading only to the tunnel at {proxy}")
        server.serve_forever()


if __name__ == "__main__":
    main()

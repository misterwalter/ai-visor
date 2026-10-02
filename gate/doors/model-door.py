#!/usr/bin/env python3
"""The model door: the one way from inside the wall to the model.

Listens on a unix socket and passes chat requests for one model on to the
model server. Everything else is refused and logged. The model server's API can
also pull, push, create and delete models, and a pull is an outbound request to
a host of the caller's choosing, so none of that may be reachable from inside
the wall. A request for another model is refused too: loading a second model
would take memory the first one needs.

Every request passed is recorded in the calls file, one JSON object per line:
what was asked (message count, tools offered) and what it cost (tokens,
seconds). The file is written out here, where the agent cannot alter it.

Each record also says how many times in a row the agent has made the very same
tool call. An agent that is stuck repeats itself, and the runner stops a run
when that number passes its limit.

A request too long for the model's window is refused with the error a hosted
model would give. Ollama itself says nothing: it quietly drops the start of the
conversation, the task included, and the model answers without it. The error
lets the harness summarise and try again, or stop, rather than carry on blind.

A model that thinks by default, as Muse-Glimmer does, thinks hardest when a
request names no reasoning level, and the harness names none when the run asked
for no thinking. Given a default level, the door adds it to a request that has
none, so that "no thinking" means none.

Usage: model-door.py <socket path> <upstream host:port> <model> <calls file> [window [reasoning]]
"""

import http.client
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time

ALLOWED_PATH = "/v1/chat/completions"
# Headers that describe one connection, not the message, and must not be passed on.
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "te", "upgrade"}
# A token is about 3.6 bytes of request as measured on real runs; dividing by 4
# errs short, so a request the model could take is never turned away.
BYTES_PER_TOKEN = 4.0
# The token counts arrive at the end of a reply, streamed or not.
TAIL_BYTES = 8192
USAGE = {name: re.compile(rb'"%s"\s*:\s*(\d+)' % name.encode()) for name in ("prompt_tokens", "completion_tokens")}


def log(text):
    sys.stderr.write(f"model-door: {text}\n")
    sys.stderr.flush()


class Door(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = None  # (host, port), set in main
    model = None
    window = 0         # the model's context window in tokens; 0 = do not check
    reasoning = None   # a reasoning level to add to a request that names none
    calls_file = None
    calls_lock = threading.Lock()
    last_move = None   # the agent's latest tool calls, as text
    repeats = 0        # how many requests in a row have carried that same move

    def log_message(self, format, *args):
        pass  # the default logger wants a client address, and a unix socket has none

    def refuse(self, reason=None):
        reason = reason or f"the wall allows only POST {ALLOWED_PATH}"
        log(f"REFUSED {self.command} {self.path}: {reason}")
        body = (reason + "\n").encode()
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def refuse_method(self):
        self.refuse()

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

    def record(self, entry):
        with self.calls_lock, open(self.calls_file, "a") as calls:
            calls.write(json.dumps(entry) + "\n")

    @classmethod
    def count_repeats(cls, request):
        """How many requests in a row, this one included, follow the same tool calls."""
        move = None
        for message in reversed(request.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "assistant":
                calls = message.get("tool_calls") or []
                move = json.dumps([[c.get("function", {}).get("name"), c.get("function", {}).get("arguments")]
                                   for c in calls if isinstance(c, dict)])
                break
        with cls.calls_lock:
            if move is not None and move != "[]" and move == cls.last_move:
                cls.repeats += 1
            else:
                cls.repeats = 1 if move not in (None, "[]") else 0
            cls.last_move = move
            return cls.repeats

    def do_POST(self):
        if self.path != ALLOWED_PATH:
            return self.refuse()
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self.refuse("a request needs a Content-Length")
        body = self.rfile.read(int(length))
        try:
            request = json.loads(body)
            asked_model = request.get("model")
            tools = [tool["function"]["name"] for tool in request.get("tools") or []]
            messages = len(request.get("messages") or [])
            repeats = self.count_repeats(request)
        except (ValueError, AttributeError, KeyError, TypeError):
            return self.refuse("the request is not a chat request")
        # An empty request is how the wall's self-check knocks; the model server rejects it.
        if asked_model is not None and asked_model != self.model:
            return self.refuse(f"this run's model is {self.model}, not {asked_model}")

        started = time.time()
        entry = {"time": time.strftime("%H:%M:%S"), "messages": messages, "tools": tools,
                 "repeats": repeats, "request_bytes": len(body), "status": None,
                 "prompt_tokens": None, "completion_tokens": None, "seconds": None}
        if self.reasoning and asked_model is not None and "reasoning_effort" not in request:
            request["reasoning_effort"] = self.reasoning
            body = json.dumps(request).encode()
            entry["reasoning"] = f"{self.reasoning}, added by the door"
        elif request.get("reasoning_effort"):
            entry["reasoning"] = request["reasoning_effort"]
        estimate = int(len(body) / BYTES_PER_TOKEN)
        if self.window and estimate > self.window:
            entry.update(status="too long", prompt_tokens=estimate, seconds=0)
            self.record(entry)
            log(f"TOO LONG: about {estimate} tokens for a {self.window}-token window; refused")
            message = (f"This model's maximum context length is {self.window} tokens. However, your messages "
                       f"resulted in about {estimate} tokens. Please reduce the length of the messages.")
            reply = json.dumps({"error": {"message": message, "type": "invalid_request_error",
                                          "code": "context_length_exceeded"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(reply)
            self.close_connection = True
            return
        upstream = http.client.HTTPConnection(*self.upstream, timeout=None)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS | {"host"}}
            upstream.request("POST", ALLOWED_PATH, body, headers)
            reply = upstream.getresponse()
            entry["status"] = reply.status
            self.send_response(reply.status)
            for key, value in reply.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            # Pass the reply on as it arrives; the harness reads it as a stream.
            tail = b""
            while True:
                chunk = reply.read1(65536)
                if not chunk:
                    break
                tail = (tail + chunk)[-TAIL_BYTES:]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            for name, pattern in USAGE.items():
                found = pattern.findall(tail)
                if found:
                    entry[name] = int(found[-1])
            log(f"passed POST {ALLOWED_PATH} -> {reply.status}")
        except (BrokenPipeError, ConnectionResetError):
            entry["status"] = "closed by the harness"
            log("the harness closed the connection before the reply ended")
        except OSError as error:
            entry["status"] = "model server unreachable"
            log(f"FAILED to reach the model server at {self.upstream}: {error}")
            try:
                self.send_error(502, "model server unreachable")
            except OSError:
                pass
        finally:
            upstream.close()  # also tells the model server to stop generating
            self.close_connection = True
            entry["seconds"] = round(time.time() - started)
            self.record(entry)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    if len(sys.argv) not in (5, 6, 7):
        sys.exit(__doc__)
    path, upstream, model, calls_file = sys.argv[1:5]
    Door.window = int(sys.argv[5]) if len(sys.argv) >= 6 else 0
    Door.reasoning = sys.argv[6] if len(sys.argv) == 7 else None
    host, _, port = upstream.rpartition(":")
    if not host or not port.isdigit():
        sys.exit(f"model-door: upstream must be host:port, got {upstream!r}")
    Door.upstream = (host, int(port))
    Door.model = model
    Door.calls_file = calls_file
    open(calls_file, "a").close()
    if os.path.exists(path):
        os.unlink(path)
    with Server(path, Door) as server:
        log(f"open at {path}, passing requests for {model} to {upstream}")
        server.serve_forever()


if __name__ == "__main__":
    main()

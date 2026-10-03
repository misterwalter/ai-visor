#!/usr/bin/env python3
"""Tests for doors/net-door.py, against a stand-in for the tunnel's SOCKS proxy
that records where it was asked to go and answers as the destination would.
The last tests run inside a real wall with no network (bubblewrap and socat),
through inside/start-agent: a program, and the wall's own check of the door.
No VPN and no internet needed.

    python3 gate/test_net_door.py
"""

import os
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
DOOR = os.path.join(HERE, "doors", "net-door.py")


class FakeTunnel(socketserver.ThreadingTCPServer):
    """Stands in for wireproxy: a SOCKS 5 proxy that connects nowhere. It records each
    destination it is asked for, then answers the request that follows as that
    destination, repeating back what it received."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.asked = []  # (how the destination was given, destination, port)
        super().__init__(("127.0.0.1", 0), FakeTunnelHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def address(self):
        return f"127.0.0.1:{self.server_address[1]}"


class FakeTunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        _, methods = s.recv(2)
        s.recv(methods)
        s.sendall(b"\x05\x00")
        _, _, _, kind = s.recv(4)
        if kind == 3:
            host, how = s.recv(s.recv(1)[0]).decode(), "name"
        else:
            host, how = socket.inet_ntop(socket.AF_INET if kind == 1 else socket.AF_INET6,
                                         s.recv(4 if kind == 1 else 16)), "number"
        (port,) = struct.unpack(">H", s.recv(2))
        self.server.asked.append((how, host, port))
        if host == "nowhere.test":
            s.sendall(b"\x05\x04\x00\x01" + bytes(6))  # host unreachable
            return
        s.sendall(b"\x05\x00\x00\x01" + bytes(6))
        received = b""
        while b"\r\n\r\n" not in received:
            chunk = s.recv(65536)
            if not chunk:
                return
            received += chunk
        body = b"the destination received:\n" + received
        s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)


class NetDoorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wall = os.path.join(self.tmp.name, "wall")
        os.mkdir(self.wall)
        self.socket = os.path.join(self.wall, "net.sock")
        self.log = os.path.join(self.tmp.name, "net-door.log")
        self.tunnel = FakeTunnel()
        self.door = None
        self.open_door(self.tunnel.address)

    def tearDown(self):
        self.door.kill()
        self.door.wait()
        self.tunnel.shutdown()
        self.tunnel.server_close()
        self.tmp.cleanup()

    def open_door(self, proxy):
        if self.door:
            self.door.kill()
            self.door.wait()
            os.unlink(self.socket)
        with open(self.log, "w") as log:
            self.door = subprocess.Popen([sys.executable, DOOR, self.socket, proxy], stderr=log)
        for _ in range(100):
            if os.path.exists(self.socket):
                return
            time.sleep(0.05)
        self.fail("the door did not open")

    def ask(self, *messages):
        """Send each message through the door in turn, waiting for an answer to each but
        the last; return everything that came back."""
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(20)
            s.connect(self.socket)
            answer = b""
            for i, message in enumerate(messages):
                s.sendall(message)
                if i < len(messages) - 1:
                    while b"\r\n\r\n" not in answer:
                        answer += s.recv(65536)
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return answer.decode()
                answer += chunk

    def logged(self):
        with open(self.log) as f:
            return f.read()

    def test_https_goes_into_the_tunnel_with_the_destination_given_by_name(self):
        answer = self.ask(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n",
                          b"GET /inside HTTP/1.1\r\nHost: example.test\r\n\r\n")
        self.assertTrue(answer.startswith("HTTP/1.1 200 Connection established"), answer)
        self.assertIn("the destination received:\nGET /inside HTTP/1.1", answer)
        # By name: the door looked nothing up, so no question left this machine outside the tunnel.
        self.assertEqual(self.tunnel.asked, [("name", "example.test", 443)])
        self.assertIn("net-door: passed CONNECT example.test:443", self.logged())

    def test_a_plain_http_request_reaches_the_destination_in_its_own_form(self):
        answer = self.ask(b"GET http://example.test:8080/page?x=1 HTTP/1.1\r\nHost: example.test:8080\r\n"
                          b"Proxy-Connection: keep-alive\r\nAccept: text/html\r\n\r\n")
        self.assertIn("GET /page?x=1 HTTP/1.1", answer)
        self.assertIn("Accept: text/html", answer)
        self.assertIn("Connection: close", answer)
        self.assertNotIn("Proxy-Connection", answer)
        self.assertEqual(self.tunnel.asked, [("name", "example.test", 8080)])
        self.assertIn("net-door: passed GET example.test:8080", self.logged())

    def test_a_request_s_body_goes_with_it(self):
        answer = self.ask(b"POST http://example.test/form HTTP/1.1\r\nHost: example.test\r\n"
                          b"Content-Length: 10\r\n\r\nname=visor")
        self.assertIn("POST /form HTTP/1.1", answer)
        self.assertTrue(answer.endswith("\r\n\r\nname=visor"), answer)

    def test_with_the_tunnel_down_every_request_fails_at_the_door(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))  # bound and never listening: nothing answers here
        self.open_door(f"127.0.0.1:{closed.getsockname()[1]}")
        for request in (b"CONNECT example.test:443 HTTP/1.1\r\n\r\n",
                        b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n"):
            answer = self.ask(request)
            self.assertTrue(answer.startswith("HTTP/1.1 502"), answer)
            self.assertIn("the VPN tunnel is not running", answer)
        closed.close()
        self.assertEqual(self.tunnel.asked, [])
        self.assertEqual(self.logged().count("net-door: FAILED"), 2)
        self.assertNotIn("passed", self.logged())

    def test_this_machine_and_private_networks_are_refused(self):
        for request in (b"CONNECT 127.0.0.1:11434 HTTP/1.1\r\n\r\n",
                        b"CONNECT localhost:11434 HTTP/1.1\r\n\r\n",
                        b"CONNECT [::1]:11434 HTTP/1.1\r\n\r\n",
                        b"CONNECT 192.168.1.5:80 HTTP/1.1\r\n\r\n",
                        b"GET http://10.0.0.1/admin HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n",
                        b"GET http://127.0.0.1:11434/api/tags HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"):
            answer = self.ask(request)
            self.assertTrue(answer.startswith("HTTP/1.1 403"), answer)
        self.assertEqual(self.tunnel.asked, [])
        self.assertEqual(self.logged().count("net-door: REFUSED"), 6)

    def test_what_is_not_asked_as_a_proxy_is_asked_is_refused(self):
        for request in (b"GET /api/tags HTTP/1.1\r\nHost: example.test\r\n\r\n",
                        b"GET https://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n",
                        b"CONNECT example.test HTTP/1.1\r\n\r\n"):
            answer = self.ask(request)
            self.assertTrue(answer.startswith("HTTP/1.1 403"), answer)
        self.assertEqual(self.tunnel.asked, [])

    def test_a_destination_the_tunnel_cannot_reach_is_said_plainly(self):
        answer = self.ask(b"CONNECT nowhere.test:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(answer.startswith("HTTP/1.1 502"), answer)
        self.assertIn("the tunnel could not reach it (host unreachable)", answer)
        self.assertIn("net-door: FAILED CONNECT nowhere.test:443", self.logged())

    def walled(self, *command):
        """Run a command as wall.sh would: on a private network with nothing on it, the wall
        dir at /run/gate, started through inside/start-agent."""
        return subprocess.run(
            ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--tmpfs", "/run", "--ro-bind", self.wall, "/run/gate",
             os.path.join(HERE, "inside", "start-agent")] + list(command),
            capture_output=True, text=True, timeout=120)

    def wall_check(self, web):
        """What the wall's own check says of the net door, for a run given the web or not."""
        checked = self.walled(os.path.join(HERE, "inside", "check-wall"), "ro", web, self.tmp.name)
        return [line for line in checked.stdout.splitlines() if "net door" in line]

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("socat"), "needs bubblewrap and socat")
    def test_inside_a_wall_with_no_network_the_door_is_the_only_way_out(self):
        program = ("import socket, urllib.request\n"
                   "print(urllib.request.urlopen('http://example.test/page', timeout=20).read().decode())\n"
                   "try:\n"
                   "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
                   "    print('DIRECT: connected')\n"
                   "except OSError as error:\n"
                   "    print('DIRECT: no way out')\n")
        inside = self.walled(sys.executable, "-c", program)
        self.assertIn("GET /page HTTP/1.1", inside.stdout, inside.stderr)
        self.assertIn("DIRECT: no way out", inside.stdout, inside.stderr)
        self.assertEqual(self.tunnel.asked, [("name", "example.test", 80)])

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("socat"), "needs bubblewrap and socat")
    def test_the_wall_s_own_check_proves_the_door_before_a_run(self):
        self.assertEqual(self.wall_check("yes"), ["ok    net door reaches the internet through the tunnel",
                                                  "ok    net door refuses this machine"])
        self.assertEqual(self.tunnel.asked, [("number", "1.1.1.1", 443)])
        # A run that was not given the web must have no door at all.
        self.assertEqual(self.wall_check("no"), ["FAIL  there is a net door, and this run was not given the web"])
        os.unlink(self.socket)
        self.assertEqual(self.wall_check("no"), ["ok    no net door"])

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("socat"), "needs bubblewrap and socat")
    def test_the_wall_s_own_check_fails_while_the_tunnel_is_down(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.open_door(f"127.0.0.1:{closed.getsockname()[1]}")
        self.assertEqual(self.wall_check("yes")[0],
                         "FAIL  net door: no way out through the VPN tunnel (got 502, expected 200)")
        closed.close()


if __name__ == "__main__":
    unittest.main()

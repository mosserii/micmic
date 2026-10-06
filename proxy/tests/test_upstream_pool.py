"""The proxy's keep-alive pool to an upstream (Jev, Gemini). After a quiet spell the
upstream has closed every idle connection; the first call after it must still go
through. It answered 502 when the one retry took a second stale pooled connection
(the first Jev and Gemini calls of a MicMic Bench v1 phone run, 2026-10-04)."""
from __future__ import annotations

import socket
import threading
import time

from proxy.app import UpstreamPool

RESPONSE = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
            b"Connection: keep-alive\r\n\r\n{}")


class DroppingUpstream:
    """Answers each request once with keep-alive, then closes the connection: what an
    upstream does to idle connections, all at once."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.served = 0
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                c.close()
                return
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        n = next((int(x.split(b":")[1]) for x in head.split(b"\r\n")
                  if x.lower().startswith(b"content-length")), 0)
        while len(rest) < n:
            rest += c.recv(4096)
        c.sendall(RESPONSE)
        self.served += 1
        time.sleep(0.05)
        c.close()

    def stop(self):
        self._stop = True
        self.sock.close()


def test_every_pooled_connection_stale_still_goes_through():
    up = DroppingUpstream()
    try:
        pool = UpstreamPool(f"http://127.0.0.1:{up.port}", timeout=3.0, size=8)
        barrier = threading.Barrier(3)
        out = []

        def call():
            barrier.wait()
            out.append(pool.post("/x", b"{}", {"Content-Type": "application/json"}))
        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert [s for s, _ in out] == [200, 200, 200]
        assert pool._idle.qsize() == 3            # three kept-alive connections pooled
        time.sleep(0.3)                           # the upstream has closed all three
        assert pool.post("/x", b"{}", {}) == (200, b"{}")
        assert pool.post("/x", b"{}", {}) == (200, b"{}")
    finally:
        up.stop()


CLOSING = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 12\r\n"
           b"Connection: close\r\n\r\n{\"ok\": true}")


def test_an_upstream_that_says_connection_close_is_read_not_502():
    """getresponse() drops conn.sock when the upstream says Connection: close; resetting
    the per-read timeout on that socket raised EBADF, so every such reply became a 502
    (every Jev call through the proxy, 2026-10-05)."""
    up = DroppingUpstream()
    global RESPONSE
    saved, RESPONSE = RESPONSE, CLOSING
    try:
        pool = UpstreamPool(f"http://127.0.0.1:{up.port}/v1", timeout=5)
        status, body = pool.post("/x", b"{}", {"Content-Type": "application/json"})
        assert status == 200 and body == b'{"ok": true}'
        status, body = pool.post("/x", b"{}", {"Content-Type": "application/json"})
        assert status == 200 and body == b'{"ok": true}'
    finally:
        RESPONSE = saved
        up.stop()

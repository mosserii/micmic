"""Findings from the adversarial lane (tests/adversarial/proxy/test_proxy_adv.py), kept
here as regressions against the fixes, plus the metering policy knobs.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from conftest import jev_body
from proxy.store import hash_token

GEM = "/v1/gemini/gemini-3.5-flash-lite:generateContent"
DAY = "2026-09-24"


def gem(**extra) -> dict:
    b = {"contents": [{"role": "user", "parts": [{"text": "SCREEN-PIXELS describe"}]}]}
    b.update(extra)
    return b


def used(p, t, kind="jev"):
    return p.store.used(hash_token(t), kind, DAY)


def raw_exchange(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


# ------------------------------------------------------------------ gemini bodies


@pytest.mark.parametrize("part", [
    {"fileData": {"fileUri": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                  "mimeType": "video/mp4"}},
    {"fileData": {"fileUri": "https://generativelanguage.googleapis.com/v1beta/files/abc123",
                  "mimeType": "application/pdf"}},
    {"file_data": {"file_uri": "gs://bucket/x.mp4"}},
    {"inlineData": {"mimeType": "video/mp4", "data": "AAAA"}},
    {"inlineData": {"mimeType": "application/pdf", "data": "AAAA"}},
    {"inlineData": {"mimeType": "image/png", "data": "AAAA", "fileUri": "https://x"}},
    {"inline_data": {"mime_type": "image/png", "data": "AAAA"}},
    {"functionCall": {"name": "x", "args": {}}},
    {"executableCode": {"code": "print(1)"}},
    {"text": "hi", "fileData": {"fileUri": "https://x"}},
    {"text": 5},
    "just a string",
])
def test_only_text_and_image_parts_reach_gemini(proxy, part):
    """HIGH finding: a fileData part makes Google fetch and bill remote media (measured:
    a 19 s YouTube video billed 1,687 prompt tokens from a 204-byte body)."""
    t = proxy.mint()
    body = {"contents": [{"parts": [{"text": "describe"}, part]}]}
    s, d, _, _ = proxy.request("POST", GEM, body, token=t)
    assert (s, d) == (400, {"error": "bad_request"})
    s, _, _, _ = proxy.request("POST", GEM, gem(systemInstruction={"parts": [part]}), token=t)
    assert s == 400
    assert proxy.upstream.hits == [] and used(proxy, t, "gemini") == 0


def test_system_instruction_is_text_only(proxy):
    t = proxy.mint()
    img = {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}
    assert proxy.request("POST", GEM, gem(systemInstruction={"parts": [img]}), token=t)[0] == 400


@pytest.mark.parametrize("extra", [
    {"tools": [{"googleSearch": {}}]}, {"toolConfig": {}}, {"cachedContent": "cachedContents/a"},
    {"tool_config": {"function_calling_config": {"mode": "ANY"}}},
    {"cached_content": "cachedContents/abc"},
    {"generation_config": {"max_output_tokens": 65536}},
    {"generationConfig": {"max_output_tokens": 65536}},
    {"generationConfig": {"cachedContent": "cachedContents/abc"}},
    {"generationConfig": {"responseModalities": ["AUDIO"]}},
    {"generationConfig": []},
    {"contents": [{"role": "system", "parts": [{"text": "x"}]}]},
    {"contents": [{"parts": [{"text": "x"}], "extra": 1}]},
    {"safetySettings": [{"category": "x", "threshold": "y", "more": 1}]},
    {"pad": "x"},
])
def test_anything_micmic_does_not_send_is_refused_not_forwarded(proxy, extra):
    t = proxy.mint()
    assert proxy.request("POST", GEM, gem(**extra), token=t)[0] == 400, extra
    assert proxy.upstream.hits == [] and used(proxy, t, "gemini") == 0


def test_micmics_own_request_shapes_pass_unchanged(proxy):
    """The bodies savta/llm.py and savta/router.py actually build."""
    t = proxy.mint()
    screen = {"contents": [{"role": "user", "parts": [
                  {"inlineData": {"mimeType": "image/png", "data": "SCREEN-PIXELS-b64"}},
                  {"text": "describe the screen"}]}],
              "systemInstruction": {"parts": [{"text": "You describe screens."}]},
              "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2}}
    text = {"contents": [{"parts": [{"text": "hello"}]}],
            "generationConfig": {"maxOutputTokens": 400, "temperature": 0.2}}
    for body in (screen, text):
        assert proxy.request("POST", GEM, body, token=t)[0] == 200
        assert json.loads(proxy.upstream.hits[-1]["body"]) == body


def test_output_clamp_covers_bools_and_candidate_count(proxy):
    t = proxy.mint()
    for v in (True, False, 100000, -1, "5000", 1.5):
        proxy.request("POST", GEM, gem(generationConfig={"maxOutputTokens": v}), token=t)
        assert json.loads(proxy.upstream.hits[-1]["body"])["generationConfig"][
            "maxOutputTokens"] == 1024, v
    proxy.store.set_plan(hash_token(t), "pro")
    proxy.request("POST", GEM, gem(generationConfig={"candidateCount": 8}), token=t)
    assert json.loads(proxy.upstream.hits[-1]["body"])["generationConfig"]["candidateCount"] == 1


# ------------------------------------------------------------------ slow clients


def test_trickled_headers_do_not_hold_threads(make_proxy):
    """idle_timeout is per recv; a byte every 0.5 s used to hold a thread for ever."""
    p = make_proxy(idle_timeout=1.0, request_timeout=1.5)
    base = threading.active_count()
    socks = []
    for _ in range(30):
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        s.sendall(b"GET /v1/usage HTTP/1.1\r\n")
        socks.append(s)
    t0 = time.time()
    while time.time() - t0 < 4:
        for s in socks:
            try:
                s.sendall(b"X")
            except OSError:
                pass
        time.sleep(0.5)
    held = threading.active_count() - base
    for s in socks:
        s.close()
    assert held < 5, f"{held} handler threads still held"


def test_trickled_body_is_cut_off_at_the_deadline(make_proxy):
    p = make_proxy(idle_timeout=1.0, request_timeout=1.5)
    t = p.mint()
    s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
    s.sendall(b"POST /v1/jev HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + t.encode()
              + b"\r\nContent-Length: 1000\r\n\r\n")
    t0 = time.time()
    closed_at = None
    while time.time() - t0 < 5:
        try:
            s.sendall(b" ")
        except OSError:
            closed_at = time.time() - t0
            break
        time.sleep(0.3)
    s.close()
    assert closed_at is not None and closed_at < 3.5, closed_at
    assert used(p, t) == 0


def test_a_slow_but_steady_upload_within_the_deadline_is_fine(make_proxy):
    p = make_proxy(idle_timeout=1.0, request_timeout=5.0)
    t = p.mint()
    body = json.dumps(jev_body()).encode()
    s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
    s.sendall(b"POST /v1/jev HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + t.encode()
              + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
    for i in range(0, len(body), len(body) // 4 + 1):
        time.sleep(0.4)
        s.sendall(body[i:i + len(body) // 4 + 1])
    s.settimeout(5)
    assert s.recv(12) == b"HTTP/1.1 200"
    s.close()


def test_keep_alive_gets_a_fresh_deadline_per_request(make_proxy):
    p = make_proxy(idle_timeout=2.0, request_timeout=1.0)
    t = p.mint()
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    for _ in range(3):
        c.request("GET", "/v1/usage", headers={"Authorization": f"Bearer {t}"})
        r = c.getresponse()
        r.read()
        assert r.status == 200
        time.sleep(0.7)                      # 2.1 s on one connection, past request_timeout
    c.close()


def test_connections_past_the_cap_get_503_and_no_thread(make_proxy):
    p = make_proxy(max_connections=3, idle_timeout=5.0)
    held = [socket.create_connection(("127.0.0.1", p.port), timeout=5) for _ in range(3)]
    time.sleep(0.3)
    # The answer is written on accept, before the request is read. (A client that has
    # already sent its request may see a reset after it, which is fine at overload.)
    out = raw_exchange(p.port, b"", timeout=3)
    assert out.startswith(b"HTTP/1.1 503") and out.endswith(b'{"error": "busy"}'), out[:60]
    for s in held:
        s.close()
    time.sleep(0.3)
    assert p.request("GET", "/health")[0] == 200


# ------------------------------------------------------------------ framing


@pytest.mark.parametrize("cl", [b"1_0", b"+10", b" 10x", b"0x10", b"-1", b"9" * 20])
def test_content_length_must_be_plain_digits(proxy, cl):
    t = proxy.mint()
    out = raw_exchange(proxy.port, b"POST /v1/jev HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                       + t.encode() + b"\r\nContent-Length: " + cl + b"\r\n"
                       b"Connection: close\r\n\r\n" + b"{}")
    assert out.startswith(b"HTTP/1.1 400"), out[:40]


def test_two_content_lengths_are_refused(proxy):
    t = proxy.mint()
    body = json.dumps(jev_body()).encode()
    out = raw_exchange(proxy.port, b"POST /v1/jev HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                       + t.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
                       + b"\r\nContent-Length: 5\r\nConnection: close\r\n\r\n" + body)
    assert out.startswith(b"HTTP/1.1 400"), out[:40]
    assert proxy.upstream.hits == []


def test_auth_scheme_is_case_insensitive(proxy):
    t = proxy.mint()
    for scheme in ("bearer", "BEARER", "Bearer"):
        assert proxy.request("GET", "/v1/usage",
                             headers={"Authorization": f"{scheme} {t}"})[0] == 200


def test_deeply_nested_json_is_a_400(proxy):
    t = proxy.mint()
    raw = b'{"questions": {"a": ' + b"[" * 100000 + b"]" * 100000 + b"}}"
    s, _, _, _ = proxy.request("POST", "/v1/jev", raw, token=t)
    assert s in (400, 413) and used(proxy, t) == 0


# ------------------------------------------------------------------ upstream


def test_upstream_200_that_is_not_json_is_502_and_refunded(proxy):
    t = proxy.mint()
    proxy.upstream.mode = "garbage200"
    s, d, raw, _ = proxy.request("POST", "/v1/jev", jev_body(), token=t)
    assert (s, d) == (502, {"error": "upstream_error"}) and b"CDN" not in raw
    assert used(proxy, t) == 0


def test_upstream_drip_is_bounded_by_one_deadline(proxy):
    t = proxy.mint()                          # jev_timeout is 1 s in the fixture
    proxy.upstream.mode = "drip"
    t0 = time.time()
    s, d, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=t)
    assert time.time() - t0 < 2.0
    assert (s, d) == (504, {"error": "upstream_timeout"})


# ------------------------------------------------------------------ metering policy


def test_timeout_keeps_its_unit_by_default_and_refunds_when_asked(make_proxy):
    """The QUESTION test's behaviour stays the default; REFUND_ON_UPSTREAM_TIMEOUT=1
    turns it round."""
    keep = make_proxy()
    t = keep.mint()
    keep.upstream.mode = "hang"
    assert keep.request("POST", "/v1/jev", jev_body(), token=t)[0] == 504
    assert used(keep, t) == 1
    give = make_proxy(refund_on_timeout=True)
    t2 = give.mint()
    assert give.request("POST", "/v1/jev", jev_body(), token=t2)[0] == 504
    assert used(give, t2) == 0


def test_units_by_size_when_configured(make_proxy):
    """Default: a call is one unit whatever its size. GEMINI_BYTES_PER_UNIT makes a big
    prompt cost more than a small one."""
    flat = make_proxy(free_daily_gemini=100)
    t = flat.mint()
    big = gem(contents=[{"parts": [{"text": "w" * 5000}]}])
    assert flat.request("POST", GEM, big, token=t)[0] == 200
    assert used(flat, t, "gemini") == 1

    sized = make_proxy(free_daily_gemini=10, gemini_bytes_per_unit=1000)
    t = sized.mint()
    assert sized.request("POST", GEM, gem(), token=t)[0] == 200           # small: 1 unit
    assert used(sized, t, "gemini") == 1
    assert sized.request("POST", GEM, big, token=t)[0] == 200             # ~5.1 KB: 6 units
    assert used(sized, t, "gemini") == 7
    s, d, _, _ = sized.request("POST", GEM, big, token=t)                 # 7 + 6 > 10
    assert s == 429 and used(sized, t, "gemini") == 7
    sized.upstream.mode = "error500"
    assert sized.request("POST", GEM, gem(), token=t)[0] == 502           # refunded in full
    assert used(sized, t, "gemini") == 7

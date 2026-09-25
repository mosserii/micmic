"""The proxy against difficult real cases, in process, with an injectable clock.

A test that asserts what the proxy SHOULD do and fails today is a finding. A test named
test_question_* asserts what it does today and exists to put a product decision on the
table; its docstring says which.
"""
from __future__ import annotations

import http.client
import json
import socket
import statistics
import threading
import time
from datetime import datetime, timezone

import pytest

from conftest import (FAKE_GEM_KEY, FAKE_JEV_KEY, GEM_PATH, gem_body, http_req,
                      jev_body)
from proxy.store import hash_token


def _raw(port: int, data: bytes, read_timeout: float = 5.0) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
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
            if b"\r\n\r\n" in out:
                head, _, rest = out.partition(b"\r\n\r\n")
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        if len(rest) >= int(line.split(b":")[1]):
                            return out
        return out
    finally:
        s.close()


# ------------------------------------------------------------------ boundaries


def test_nth_call_succeeds_and_n_plus_1_is_refused(inproc, upstream):
    p = inproc()
    t = p.mint()
    for i in range(5):
        s, d, _, _ = p.req("POST", "/v1/jev", jev_body(), t)
        assert s == 200, (i, d)
    s, d, _, h = p.req("POST", "/v1/jev", jev_body(), t)
    assert s == 429
    assert d == {"error": "daily_limit", "limit": 5, "resets_at": "2026-09-25T00:00:00Z"}
    assert h["Retry-After"] == str(12 * 3600)
    assert p.used(t) == 5
    assert len(upstream.hits) == 5, "the refused call must never reach the upstream"


def test_gemini_cap_is_independent_of_jev_cap(inproc):
    p = inproc()
    t = p.mint()
    for _ in range(3):
        assert p.req("POST", GEM_PATH, gem_body(), t)[0] == 200
    assert p.req("POST", GEM_PATH, gem_body(), t)[0] == 429
    for _ in range(5):
        assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 200
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 429
    u = p.req("GET", "/v1/usage", token=t)[1]
    assert u["used_today"] == 5 and u["gemini"]["used_today"] == 3


def test_plan_change_mid_day_takes_effect_on_next_call(inproc):
    p = inproc()
    t = p.mint("free")
    h = hash_token(t)
    for _ in range(5):
        p.req("POST", "/v1/jev", jev_body(), t)
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 429
    p.store.set_plan(h, "pro")
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 200
    u = p.req("GET", "/v1/usage", token=t)[1]
    assert (u["plan"], u["used_today"], u["limit_today"]) == ("pro", 6, 50)
    # and back down: already past the free cap, so refused at once
    p.store.set_plan(h, "free")
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 429


def test_utc_midnight_rollover(inproc):
    p = inproc()
    t = p.mint()
    p.clock.now = datetime(2026, 9, 24, 23, 59, 59, 900000, tzinfo=timezone.utc)
    for _ in range(5):
        p.req("POST", "/v1/jev", jev_body(), t)
    s, d, _, h = p.req("POST", "/v1/jev", jev_body(), t)
    assert s == 429 and d["resets_at"] == "2026-09-25T00:00:00Z"
    assert h["Retry-After"] == "1"
    p.clock.now = datetime(2026, 9, 25, 0, 0, 0, tzinfo=timezone.utc)
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 200
    u = p.req("GET", "/v1/usage", token=t)[1]
    assert u["used_today"] == 1 and u["resets_at"] == "2026-09-26T00:00:00Z"
    # month and year edges
    for now, want in ((datetime(2026, 9, 30, 22, tzinfo=timezone.utc), "2026-10-01T00:00:00Z"),
                      (datetime(2026, 12, 31, 23, tzinfo=timezone.utc), "2027-01-01T00:00:00Z"),
                      (datetime(2028, 2, 28, 23, tzinfo=timezone.utc), "2028-02-29T00:00:00Z")):
        p.clock.now = now
        assert p.req("GET", "/v1/usage", token=t)[1]["resets_at"] == want


def test_refund_across_midnight_goes_back_to_the_day_it_was_taken(inproc, upstream):
    """Taken at 23:59:59.9, upstream fails after midnight: the unit must go back to the
    day it came from, not be subtracted from a fresh day (where n=0 blocks it anyway)."""
    p = inproc()
    t = p.mint()
    p.clock.now = datetime(2026, 9, 24, 23, 59, 59, 900000, tzinfo=timezone.utc)
    upstream.mode = "status:500"
    orig = upstream.srv.RequestHandlerClass.do_POST

    def slow(self):
        p.clock.now = datetime(2026, 9, 25, 0, 0, 1, tzinfo=timezone.utc)
        return orig(self)
    upstream.srv.RequestHandlerClass.do_POST = slow
    try:
        assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 502
    finally:
        upstream.srv.RequestHandlerClass.do_POST = orig
    assert p.used(t, day="2026-09-24") == 0
    assert p.used(t, day="2026-09-25") == 0


def test_question_client_chosen_jev_model_is_forwarded_verbatim(inproc, upstream):
    """QUESTION: Gemini has a model allowlist so a leaked token cannot aim the owner's key
    at the priciest model. Jev has none: the "model" field in the body goes to Jev as the
    client wrote it. If Jev ever prices models differently, this is the same hole."""
    p = inproc()
    t = p.mint()
    body = jev_body()
    body["model"] = "jev-most-expensive-model"
    assert p.req("POST", "/v1/jev", body, t)[0] == 200
    assert json.loads(upstream.hits[-1]["body"])["model"] == "jev-most-expensive-model"


# ------------------------------------------------------------------ auth


def test_auth_matrix(inproc, upstream):
    p = inproc()
    t = p.mint()
    cases = {
        "missing": {},
        "empty bearer": {"Authorization": "Bearer "},
        "basic": {"Authorization": f"Basic {t}"},
        "no scheme": {"Authorization": t},
        "wrong": {"Authorization": "Bearer mmp_" + "A" * 43},
        "internal space": {"Authorization": f"Bearer {t[:10]} {t[10:]}"},
        "too long": {"Authorization": "Bearer " + t + "x" * 300},
        "x-api-key header": {"X-Api-Key": t},
        "goog header": {"X-goog-api-key": t},
    }
    for name, h in cases.items():
        s, d, _, _ = p.req("POST", "/v1/jev", jev_body(), headers=h)
        assert s == 401, name
    for q in ("token", "access_token", "key", "auth"):
        s, _, _, _ = p.req("POST", f"/v1/jev?{q}={t}", jev_body())
        assert s == 401, f"token in ?{q}= must not authenticate"
        s, _, _, _ = p.req("GET", f"/v1/usage?{q}={t}")
        assert s == 401
    assert p.used(t) == 0 and upstream.hits == []
    # surrounding whitespace is forgiven, which is fine
    assert p.req("POST", "/v1/jev", jev_body(),
                 headers={"Authorization": f"Bearer   {t}\t"})[0] == 200


def test_auth_scheme_is_case_insensitive(inproc):
    """RFC 9110 11.1: the auth scheme is case-insensitive. A client or an intermediary
    that normalises to "bearer" gets a 401 it cannot explain. Low severity: MicMic's own
    client always sends "Bearer"."""
    p = inproc()
    t = p.mint()
    assert p.req("GET", "/v1/usage", headers={"Authorization": f"bearer {t}"})[0] == 200


def test_oversized_header_is_a_clean_4xx(inproc):
    p = inproc()
    s, _, _, _ = p.req("GET", "/v1/usage", headers={"Authorization": "Bearer " + "a" * 70000})
    assert s in (400, 431)


def test_revoked_mid_session_on_a_kept_alive_connection(inproc):
    p = inproc()
    t = p.mint()
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    assert http_req(p.port, "POST", "/v1/jev", jev_body(), t, conn=c)[0] == 200
    assert p.store.revoke(hash_token(t))
    assert http_req(p.port, "POST", "/v1/jev", jev_body(), t, conn=c)[0] == 401
    c.close()
    assert p.used(t) == 1


def test_wrong_token_timing_is_not_distinguishable(inproc):
    """Measured on /v1/usage (no upstream). Medians of 300 each, interleaved."""
    p = inproc()
    t = p.mint()
    for _ in range(200):
        p.store.mint("free")          # a realistic table, not a single row
    wrong_same_prefix = t[:20] + ("A" if t[20] != "A" else "B") + t[21:]
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    good, bad, bad2 = [], [], []
    for _ in range(300):
        for tok, bucket in ((t, good), ("mmp_" + "Z" * 43, bad), (wrong_same_prefix, bad2)):
            t0 = time.perf_counter()
            http_req(p.port, "GET", "/v1/usage", token=tok, conn=c)
            bucket.append(time.perf_counter() - t0)
    c.close()
    g, b, b2 = (statistics.median(x) for x in (good, bad, bad2))
    print(f"median ms right={g*1e3:.3f} wrong={b*1e3:.3f} wrong-same-prefix={b2*1e3:.3f}")
    # A 401 skips the usage queries, so it is somewhat faster; what matters is that a
    # near-miss token is not measurably closer to the right one than a random one.
    assert abs(b2 - b) / b < 0.25


# ------------------------------------------------------------------ abuse


def test_body_limits_are_exact_and_not_metered(inproc, upstream):
    p = inproc(max_jev_body=1000, max_gemini_body=2000)
    t = p.mint()

    def padded(n, base):
        # Pad inside text MicMic really sends: the proxy now refuses unknown keys, so
        # an extra "pad" key would be a 400, not a size test.
        raw = json.dumps(base)
        pad = n - len(raw)
        if "contents" in base:
            base["contents"][0]["parts"][0]["text"] += "x" * pad
        else:
            base["state"]["utterance"] += "x" * pad
        out = json.dumps(base).encode()
        assert len(out) == n
        return out
    assert p.req("POST", "/v1/jev", padded(1000, jev_body()), t)[0] == 200
    s, d, _, _ = p.req("POST", "/v1/jev", padded(1001, jev_body()), t)
    assert s == 413 and d["max_bytes"] == 1000
    assert p.req("POST", GEM_PATH, padded(2000, gem_body()), t)[0] == 200
    assert p.req("POST", GEM_PATH, padded(2001, gem_body()), t)[0] == 413
    assert p.used(t) == 1 and p.used(t, "gemini") == 1
    assert len(upstream.hits) == 2


def test_gemini_model_allowlist(inproc, upstream):
    p = inproc()
    t = p.mint()
    for m in ("gemini-2.5-pro", "gemini-3.5-flash-lite-preview", "GEMINI-3.5-FLASH-LITE",
              "gemini-3.5-flash-lite%00", "gemini-3.5-flash-lite.", "..", "."):
        s, _, _, _ = p.req("POST", f"/v1/gemini/{m}:generateContent", gem_body(), t)
        assert s in (400, 404), m
    for path in (f"/v1/gemini/gemini-3.5-flash-lite:streamGenerateContent",
                 f"/v1/gemini/gemini-3.5-flash-lite:countTokens",
                 "/v1/gemini/gemini-3.5-flash-lite%3AgenerateContent",
                 "/v1/gemini/../jev", "/v1/jev/", "//v1/jev", "/v1/jev/../jev",
                 "/V1/JEV", "/v1/gemini/gemini-3.5-flash-lite:generateContent/x",
                 "/v1/gemini/models/gemini-3.5-flash-lite:generateContent"):
        s, _, _, _ = p.req("POST", path, gem_body(), t)
        assert s in (400, 404), path
    assert upstream.hits == [] and p.used(t, "gemini") == 0 and p.used(t) == 0


def test_absolute_form_request_target_is_not_routed(inproc, upstream):
    p = inproc()
    t = p.mint()
    body = json.dumps(jev_body()).encode()
    out = _raw(p.port, b"POST http://evil.example/v1/jev HTTP/1.1\r\nHost: x\r\n"
               b"Authorization: Bearer " + t.encode() + b"\r\nContent-Length: "
               + str(len(body)).encode() + b"\r\n\r\n" + body)
    assert out.startswith(b"HTTP/1.1 404")
    assert upstream.hits == []


def test_max_output_tokens_is_clamped(inproc, upstream):
    p = inproc(free_daily_gemini=100)
    t = p.mint()
    for v in (100000, -1, 0, "5000", 1.5e9, None):
        assert p.req("POST", GEM_PATH, gem_body(generationConfig={"maxOutputTokens": v}),
                     t)[0] == 200
        assert json.loads(upstream.hits[-1]["body"])["generationConfig"][
            "maxOutputTokens"] == 1024, v


def test_max_output_tokens_boolean_passes_the_clamp(inproc, upstream):
    """isinstance(True, int) is True in Python, so `true` reaches Gemini unclamped.
    Harmless today (Gemini rejects it, and a rejection is refunded) but it shows the
    check is by Python type, not by JSON type. Low."""
    p = inproc()
    t = p.mint()
    p.req("POST", GEM_PATH, gem_body(generationConfig={"maxOutputTokens": True}), t)
    assert json.loads(upstream.hits[-1]["body"])["generationConfig"]["maxOutputTokens"] == 1024


def test_snake_case_generation_config_bypasses_the_output_clamp(inproc, upstream):
    """Was Low (defence in depth): generation_config / max_output_tokens slipped past a
    clamp keyed on one spelling. Fixed in the proxy by refusing every key MicMic does
    not send, so both spellings are now a 400 and nothing reaches Gemini."""
    p = inproc()
    t = p.mint()
    before = len(upstream.hits)
    s, _, _, _ = p.req("POST", GEM_PATH, gem_body(
        generation_config={"max_output_tokens": 65536}), t)
    assert s == 400
    s, _, _, _ = p.req("POST", GEM_PATH, gem_body(generationConfig={"max_output_tokens": 65536}), t)
    assert s == 400
    assert len(upstream.hits) == before and p.used(t, "gemini") == 0


def test_forbidden_features_via_snake_case(inproc, upstream):
    """Low. GEMINI_FORBIDDEN lists toolConfig and cachedContent; the snake_case spellings
    tool_config and cached_content pass. cached_content names state on the OWNER's
    account, which is what the list exists to keep out (an attacker would need a cache
    name, and the owner creates none today)."""
    p = inproc()
    t = p.mint()
    for extra in ({"cached_content": "cachedContents/abc"},
                  {"tool_config": {"function_calling_config": {"mode": "ANY"}}},
                  {"generationConfig": {"cachedContent": "cachedContents/abc"}}):
        s, _, _, _ = p.req("POST", GEM_PATH, gem_body(**extra), t)
        assert s == 400, (extra, "forwarded")


def test_candidate_count_multiplies_the_output_cap(inproc, upstream):
    """Low (defence in depth). maxOutputTokens caps each candidate, so candidateCount
    multiplies the cap. The real gemini-3.5-flash-lite answers 400 to candidateCount 4
    today (test_real_gemini.py), so not exploitable on the current model allowlist."""
    p = inproc()
    t = p.mint()
    p.req("POST", GEM_PATH, gem_body(generationConfig={"candidateCount": 8}), t)
    sent = json.loads(upstream.hits[-1]["body"])["generationConfig"]
    assert sent.get("candidateCount", 1) <= 1, sent


def test_file_uri_parts_are_forwarded(inproc, upstream):
    """SECURITY/cost, HIGH. A part {"fileData": {"fileUri": ...}} makes Gemini fetch the
    file itself. Measured on the real Gemini through this proxy (test_real_gemini.py):
    a 204-byte body naming a 19 s YouTube video was billed 1,687 prompt tokens (1,676 of
    them VIDEO), ~88 tokens per second of footage, and max_gemini_body bounds none of
    it. An hour-long video is ~300k tokens per call, 100 calls a day on a FREE token. A
    files/... URI would read files uploaded to the OWNER's account. MicMic only ever
    sends text and inlineData, so rejecting fileData costs the product nothing."""
    p = inproc()
    t = p.mint()
    for uri in ("https://www.youtube.com/watch?v=aaaaaaaaaaa",
                "https://generativelanguage.googleapis.com/v1beta/files/abc123"):
        body = {"contents": [{"parts": [{"text": "describe"},
                                        {"fileData": {"fileUri": uri,
                                                      "mimeType": "video/mp4"}}]}]}
        s, _, _, _ = p.req("POST", GEM_PATH, body, t)
        assert s == 400, f"fileData {uri} was forwarded"


def test_question_prompt_size_is_bounded_by_bytes_not_tokens(inproc, upstream):
    """QUESTION/cost. The daily cap counts calls, not tokens. A free token may send 100
    Gemini calls of up to 8 MB each; 7 MB of text is roughly 1.7M tokens (past the
    context window, so rejected and refunded), but ~3.5 MB (~0.9M tokens) is accepted
    and billed. Jev likewise: 300 calls x 256 KB. Asserts today's behaviour."""
    p = inproc(max_gemini_body=8 * 1024 * 1024)
    t = p.mint()
    body = {"contents": [{"parts": [{"text": "word " * (700 * 1024)}]}]}   # 3.5 MB
    assert p.req("POST", GEM_PATH, body, t)[0] == 200
    assert len(upstream.hits[-1]["body"]) > 3_000_000


def test_non_json_bodies_are_rejected_and_not_metered(inproc, upstream):
    p = inproc()
    t = p.mint()
    for raw in (b"not json", b"[1,2,3]", b'"str"', b"\xff\xfe\x00", b"", b"null",
                b'{"questions": []}', b'{"questions": "x"}', b"{" * 5000,
                b'{"questions": {}, "questions": 5}'):
        s, _, _, _ = p.req("POST", "/v1/jev", raw, t,
                           headers={"Content-Type": "application/json"})
        assert s == 400, raw[:20]
    for raw in (b'{"contents": {}}', b'{"contents": [], "tools": []}',
                b'{"contents": [], "generationConfig": []}'):
        assert p.req("POST", GEM_PATH, raw, t)[0] == 400, raw
    assert upstream.hits == [] and p.used(t) == 0 and p.used(t, "gemini") == 0


def test_deeply_nested_json_does_not_crash_the_handler(inproc, upstream):
    p = inproc()
    t = p.mint()
    raw = b'{"questions": {"a": ' + b"[" * 100000 + b"]" * 100000 + b"}}"
    s, d, _, _ = p.req("POST", "/v1/jev", raw, t)
    assert s in (400, 413), (s, d)
    assert p.used(t) == 0


def test_content_length_forms_python_int_accepts(inproc, upstream):
    """int() accepts "1_0", "+10" and " 10 ". A front proxy that reads the same header
    differently (most reject "1_0") is the setup for request smuggling. Low while the
    proxy is directly exposed; worth a strict ^[0-9]+$ check before it sits behind one."""
    p = inproc()
    t = p.mint()
    body = json.dumps(jev_body()).encode()
    n = len(body)
    weird = f"{str(n)[:1]}_{str(n)[1:]}"
    out = _raw(p.port, b"POST /v1/jev HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
               + t.encode() + b"\r\nContent-Length: " + weird.encode() + b"\r\n"
               b"Connection: close\r\n\r\n" + body)
    assert out.startswith(b"HTTP/1.1 400"), out[:40]


def test_duplicate_content_length_is_rejected(inproc, upstream):
    """Two Content-Length headers that disagree must be a 400 (RFC 9112 6.3). The proxy
    reads the first and would parse the rest of the body as the next request on the
    connection: a smuggled second call, authenticated by whatever it carries."""
    p = inproc()
    t = p.mint()
    body = json.dumps(jev_body()).encode()
    out = _raw(p.port, b"POST /v1/jev HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
               + t.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
               + b"\r\nContent-Length: 5\r\nConnection: close\r\n\r\n" + body)
    assert out.startswith(b"HTTP/1.1 400"), out[:40]


def test_crlf_in_path_is_not_reflected(inproc):
    p = inproc()
    out = _raw(p.port, b"GET /v1/usage%0d%0aX-Injected:%201 HTTP/1.1\r\nHost: x\r\n"
               b"Connection: close\r\n\r\n")
    assert b"X-Injected" not in out.split(b"\r\n\r\n")[0]
    out = _raw(p.port, b"GET /nope\r\nX-Injected: 1 HTTP/1.1\r\nHost: x\r\n"
               b"Connection: close\r\n\r\n")
    assert b"\r\nX-Injected" not in out.split(b"\r\n\r\n")[0]


def test_unsupported_methods(inproc):
    p = inproc()
    for m in ("PUT", "DELETE", "PATCH"):
        assert p.req(m, "/v1/jev", b"{}")[0] == 405
    for m in ("HEAD", "OPTIONS", "TRACE", "CONNECT"):
        s = p.req(m, "/v1/jev")[0] if m != "HEAD" else None
        if s is not None:
            assert s in (405, 501), m


# ------------------------------------------------------------------ slow clients


def test_idle_connection_is_closed_after_idle_timeout(inproc):
    p = inproc(idle_timeout=1.0)
    s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
    t0 = time.time()
    got = s.recv(10)
    elapsed = time.time() - t0
    s.close()
    assert got == b"" and elapsed < 3, elapsed


def test_slowloris_headers_hold_a_thread_past_idle_timeout(inproc):
    """SECURITY/availability. idle_timeout is a per-recv socket timeout, not a deadline
    for the request. A client that sends one header byte every 0.5 s keeps its handler
    thread for as long as it likes, and ThreadingHTTPServer starts a thread for every
    connection with no ceiling. With idle_timeout=1 s, 100 such clients held 100 threads
    for 6 s here; in production (60 s) nothing ever reclaims them."""
    p = inproc(idle_timeout=1.0)
    base = threading.active_count()
    socks = []
    for _ in range(100):
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        s.sendall(b"GET /v1/usage HTTP/1.1\r\n")
        socks.append(s)
    alive_at_end = 0
    t0 = time.time()
    while time.time() - t0 < 6:
        for s in socks:
            try:
                s.sendall(b"X")
            except OSError:
                pass
        time.sleep(0.5)
    held = threading.active_count() - base
    for s in socks:
        try:
            s.sendall(b"-a: b\r\n")
            s.settimeout(0.05)
            if s.recv(1) == b"":
                pass
        except socket.timeout:
            alive_at_end += 1
        except OSError:
            pass
        s.close()
    print(f"threads held after 6 s of 1-byte trickle with idle_timeout=1 s: {held}; "
          f"sockets still open: {alive_at_end}/100")
    assert held < 10, f"{held} handler threads still held 6x past idle_timeout"


def test_slow_body_holds_a_thread_past_idle_timeout(inproc):
    """Same, on the body: Content-Length 1000, one byte every 0.5 s."""
    p = inproc(idle_timeout=1.0)
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
        time.sleep(0.5)
    s.close()
    assert closed_at is not None and closed_at < 3, \
        f"body trickle kept the connection for {closed_at or '>5'} s with idle_timeout=1 s"


# ------------------------------------------------------------------ upstream failures


@pytest.mark.parametrize("mode,want,metered", [
    ("status:500", 502, 0),
    ("status:401", 502, 0),
    ("status:400", 400, 0),
    ("status:429", 503, 0),
    ("status:503", 503, 0),
    ("garbage", 502, 0),
    ("close_mid_body", 502, 0),
])
def test_upstream_failure_is_answered_and_refunded(inproc, upstream, mode, want, metered):
    p = inproc()
    t = p.mint()
    upstream.mode = mode
    s, d, raw, h = p.req("POST", "/v1/jev", jev_body(), t)
    assert s == want, (mode, s, d)
    assert FAKE_JEV_KEY.encode() not in raw and "fake" not in json.dumps(h)
    assert p.used(t) == metered
    s, d, raw, h = p.req("POST", GEM_PATH, gem_body(), t)
    assert s == want and p.used(t, "gemini") == metered
    assert FAKE_GEM_KEY.encode() not in raw


def test_question_upstream_timeout_is_metered(inproc, upstream):
    """QUESTION: a hung upstream answers 504 and the unit is NOT refunded (by design: the
    upstream may have done and billed the work). Combined with the client's retry
    (test_client_timeout_retries_burn_three_units) one hang costs her three units."""
    p = inproc()
    t = p.mint()
    upstream.mode, upstream.hang = "hang", 2.5
    t0 = time.time()
    s, d, _, _ = p.req("POST", "/v1/jev", jev_body(), t)
    assert s == 504 and d == {"error": "upstream_timeout"}
    assert time.time() - t0 < 2.0
    assert p.used(t) == 1


def test_upstream_200_garbage_is_passed_on_and_metered(inproc, upstream):
    """The proxy relays a 200 whose body is not JSON (a CDN error page, say) and keeps
    the unit. The client cannot parse it, counts it as a failure and retries."""
    p = inproc()
    t = p.mint()
    upstream.mode = "garbage200"
    s, _, raw, _ = p.req("POST", "/v1/jev", jev_body(), t)
    assert s == 502, f"relayed {s} {raw[:40]!r} and metered {p.used(t)}"


def test_upstream_timeout_is_a_deadline_not_per_read(inproc, upstream):
    """jev_timeout=1 s. An upstream that drips its body one byte per 0.5 s is never
    idle for a whole second, so http.client never times out and the proxy waits as long
    as the drip lasts. Here 8 bytes took ~4 s; an endless drip pins the thread and the
    pooled connection forever. Low: the upstreams are Google and Jev."""
    p = inproc()
    t = p.mint()
    upstream.mode, upstream.drip_n, upstream.drip_every = "drip", 8, 0.5
    t0 = time.time()
    s, _, _, _ = p.req("POST", "/v1/jev", jev_body(), t)
    took = time.time() - t0
    print(f"status {s} after {took:.1f} s with jev_timeout=1 s")
    assert took < 2.0, f"answered after {took:.1f} s, timeout is 1 s"


def test_client_timeout_retries_burn_three_units(inproc, upstream, monkeypatch):
    """FAILURE. savta.jev.Jev times out at 12 s and retries twice; the proxy waits 15 s
    on Jev and does not refund a timeout. So the client always gives up first, and every
    attempt is a unit that is never given back: one hung Jev call costs three of her
    300 and ~36 s of silence. Scaled down here: client 1 s, proxy 3 s, upstream 5 s."""
    p = inproc(jev_timeout=3.0)
    t = p.mint()
    upstream.mode, upstream.hang = "hang", 5.0
    monkeypatch.setenv("MICMIC_MODE", "proxy")
    monkeypatch.setenv("MICMIC_PROXY_URL", p.url)
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", t)
    from savta.jev import Jev
    j = Jev(timeout=1.0)
    t0 = time.time()
    with pytest.raises(RuntimeError):
        j.ask({"utterance": "what time is it"}, jev_body()["questions"])
    took = time.time() - t0
    time.sleep(3.5)                   # let the proxy's own waits run out
    print(f"client gave up after {took:.1f} s; units used {p.used(t)}; "
          f"upstream hits {len(upstream.hits)}")
    assert p.used(t) <= 1, f"{p.used(t)} units for one question"


def test_client_retries_on_upstream_busy_are_refunded(inproc, upstream, monkeypatch):
    p = inproc()
    t = p.mint()
    upstream.mode = "status:429"
    monkeypatch.setenv("MICMIC_MODE", "proxy")
    monkeypatch.setenv("MICMIC_PROXY_URL", p.url)
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", t)
    from savta.jev import Jev
    with pytest.raises(RuntimeError):
        Jev(timeout=2).ask({"u": 1}, jev_body()["questions"])
    assert len(upstream.hits) == 3 and p.used(t) == 0


def test_client_retries_on_200_garbage_burn_units(inproc, upstream, monkeypatch):
    """FAILURE (follows from test_upstream_200_garbage_is_passed_on_and_metered)."""
    p = inproc()
    t = p.mint()
    upstream.mode = "garbage200"
    monkeypatch.setenv("MICMIC_MODE", "proxy")
    monkeypatch.setenv("MICMIC_PROXY_URL", p.url)
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", t)
    from savta.jev import Jev
    with pytest.raises(RuntimeError):
        Jev(timeout=2).ask({"u": 1}, jev_body()["questions"])
    print(f"units used for one failed question: {p.used(t)}")
    assert p.used(t) <= 1


def test_device_token_never_goes_upstream(inproc, upstream):
    p = inproc()
    t = p.mint()
    p.req("POST", "/v1/jev", jev_body(), t, headers={"X-Forwarded-For": t,
                                                     "Cookie": f"t={t}"})
    p.req("POST", GEM_PATH, gem_body(), t)
    jh, gh = upstream.hits[0], upstream.hits[1]
    assert jh["headers"]["Authorization"] == f"Bearer {FAKE_JEV_KEY}"
    assert gh["headers"]["X-goog-api-key"] == FAKE_GEM_KEY
    for h in upstream.hits:
        blob = json.dumps(h["headers"]) + h["body"].decode() + h["path"]
        assert t not in blob and hash_token(t) not in blob
        assert "Cookie" not in h["headers"] and "X-Forwarded-For" not in h["headers"]

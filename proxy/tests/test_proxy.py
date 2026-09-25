"""The proxy against a fake upstream. Nothing here costs money or leaves the machine."""
from __future__ import annotations

import http.client
import json
import multiprocessing
import socket
import threading
from datetime import datetime, timezone

from conftest import (FAKE_GEMINI_KEY, FAKE_JEV_KEY, GEMINI_ANSWER, JEV_ANSWER,
                      assert_no_leak, jev_body)
from proxy import admin
from proxy.store import Store, hash_token

GEM = "/v1/gemini/gemini-3.5-flash-lite:generateContent"


def gem_body(marker="SCREEN-PIXELS-base64data"):
    return {"contents": [{"role": "user", "parts": [
        {"text": "describe the screen"},
        {"inlineData": {"mimeType": "image/png", "data": marker}}]}]}


# ------------------------------------------------------------------ auth

def test_health_needs_no_token(proxy):
    s, d, _, _ = proxy.request("GET", "/healthz")
    assert (s, d) == (200, {"ok": True})


def test_missing_wrong_and_revoked_tokens_look_identical(proxy):
    good = proxy.mint()
    revoked = proxy.mint()
    assert proxy.store.revoke(hash_token(revoked))
    answers = []
    for tok in (None, "", "mmp_not-a-real-token", good + "x", revoked, "x" * 300):
        headers = {} if tok is None else {"Authorization": f"Bearer {tok}"}
        s, d, raw, h = proxy.request("POST", "/v1/jev", jev_body(), headers=headers)
        answers.append((s, raw))
        assert h.get("WWW-Authenticate") == "Bearer"
    # Same status, same bytes: nothing tells a caller whether a token ever existed.
    assert {a for a in answers} == {(401, b'{"error": "unauthorized"}')}
    s, _, _, _ = proxy.request("POST", "/v1/jev", jev_body(), headers={"Authorization": good})
    assert s == 401                                   # no "Bearer " scheme
    s, _, _, _ = proxy.request("GET", "/v1/usage", token=revoked)
    assert s == 401
    assert proxy.upstream.hits == []                  # nothing unauthenticated went out


# ------------------------------------------------------------------ bodies

def test_malformed_json_is_400_and_not_metered(proxy):
    tok = proxy.mint()
    for body in (b"{not json", b"", b"\xff\xfe", b"[1,2,3]", b'"a string"', b"null",
                 json.dumps({"state": "x"}).encode()):          # no questions
        s, d, _, _ = proxy.request("POST", "/v1/jev", body, token=tok)
        assert s == 400, body
        assert d["error"] in ("bad_json", "bad_request")
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 0
    assert proxy.upstream.hits == []


def test_oversized_body_is_413_before_reading_and_not_metered(proxy):
    tok = proxy.mint()
    big = jev_body("SAID-OUT-LOUD-" + "a" * (70 * 1024))
    s, d, _, _ = proxy.request("POST", "/v1/jev", big, token=tok)
    assert s == 413 and d == {"error": "body_too_large", "max_bytes": 64 * 1024}
    # A body far past the drain limit: the proxy answers and hangs up without reading it.
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    c.putrequest("POST", "/v1/jev")
    c.putheader("Authorization", f"Bearer {tok}")
    c.putheader("Content-Length", str(1 << 30))
    c.endheaders()
    r = c.getresponse()
    assert r.status == 413
    c.close()
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 0
    assert proxy.upstream.hits == []


def test_chunked_or_lengthless_body_is_refused(proxy):
    tok = proxy.mint()
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    c.request("POST", "/v1/jev", body=iter([b'{"questions":', b"{}}"]),
              headers={"Authorization": f"Bearer {tok}", "Transfer-Encoding": "chunked"},
              encode_chunked=True)
    assert c.getresponse().status == 411
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    c.putrequest("POST", "/v1/jev")                  # no Content-Length at all
    c.putheader("Authorization", f"Bearer {tok}")
    c.endheaders()
    assert c.getresponse().status == 411
    c.close()


def test_unknown_routes_and_methods(proxy):
    tok = proxy.mint()
    assert proxy.request("GET", "/v1/jev", token=tok)[0] == 404
    assert proxy.request("POST", "/v1/other", jev_body(), token=tok)[0] == 404
    assert proxy.request("PUT", "/v1/jev", jev_body(), token=tok)[0] == 405
    assert proxy.request("GET", "/nope", token=tok)[0] == 404


# ------------------------------------------------------------------ forwarding

def test_jev_forwards_body_and_returns_upstream_bytes_unchanged(proxy):
    tok = proxy.mint()
    body = jev_body()
    s, d, raw, headers = proxy.request("POST", "/v1/jev", body, token=tok)
    assert s == 200 and d == JEV_ANSWER
    assert raw == json.dumps(JEV_ANSWER).encode()
    hit = proxy.upstream.hits[0]
    assert hit["path"] == "/v1/systemone"
    assert json.loads(hit["body"]) == body
    assert hit["headers"]["Authorization"] == f"Bearer {FAKE_JEV_KEY}"
    # The device token is ours; it never goes to Jev or Google.
    assert tok not in json.dumps(hit["headers"]) and tok.encode() not in hit["body"]
    # Upstream headers are not relayed.
    assert "X-Upstream-Secret" not in headers


def test_keep_alive_serves_several_requests_on_one_connection(proxy):
    tok = proxy.mint()
    c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    for _ in range(3):
        s, d, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok, conn=c)
        assert s == 200
    # An error that leaves the body unread must not poison the next request either.
    s, _, _, _ = proxy.request("POST", "/v1/jev", b"{bad", token=tok, conn=c)
    assert s == 400
    s, _, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok, conn=c)
    assert s == 200
    c.close()


# ------------------------------------------------------------------ metering

def test_exactly_at_cap_then_one_over(proxy):
    tok = proxy.mint()                              # free cap is 5 in these tests
    for i in range(5):
        s, _, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
        assert s == 200, i
    s, d, _, h = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 429
    assert d == {"error": "daily_limit", "limit": 5, "resets_at": "2026-09-25T00:00:00Z",
                 "scope": "day"}
    assert h["Retry-After"] == str(12 * 3600)
    assert len(proxy.upstream.hits) == 5            # the over-cap call never went out
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 5


def test_pro_plan_has_its_own_cap(proxy):
    tok = proxy.mint("pro")
    for _ in range(6):
        assert proxy.request("POST", "/v1/jev", jev_body(), token=tok)[0] == 200
    s, d, _, _ = proxy.request("GET", "/v1/usage", token=tok)
    assert d["plan"] == "pro" and d["limit_today"] == 50 and d["used_today"] == 6


def test_fifty_concurrent_requests_never_exceed_the_cap(proxy):
    proxy.cfg.free_daily_calls = 10
    tok = proxy.mint()
    h = hash_token(tok)
    for _ in range(7):                               # 3 units left
        assert proxy.store.consume(h, "jev", "2026-09-24", 10)[0]
    proxy.upstream.delay = 0.05                      # hold each call open so they overlap
    barrier = threading.Barrier(50)
    results: list[int] = []
    lock = threading.Lock()

    def one():
        c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=20)
        body = json.dumps(jev_body()).encode()
        barrier.wait()
        c.request("POST", "/v1/jev", body=body, headers={"Authorization": f"Bearer {tok}"})
        r = c.getresponse()
        r.read()
        with lock:
            results.append(r.status)
        c.close()

    threads = [threading.Thread(target=one) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200] * 3 + [429] * 47
    assert proxy.store.used(h, "jev", "2026-09-24") == 10
    assert len(proxy.upstream.hits) == 3


def _hammer(db, h, n, limit, q):
    s = Store(db)
    q.put(sum(1 for _ in range(n) if s.consume(h, "jev", "2026-09-24", limit)[0]))


def test_cap_holds_across_processes_too(tmp_path):
    """A second server process on the same database must not break the cap either."""
    db = tmp_path / "p.db"
    h = hash_token(Store(db).mint("free"))
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_hammer, args=(db, h, 40, 97, q)) for _ in range(4)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
    granted = sum(q.get(timeout=5) for _ in ps)
    assert granted == 97
    assert Store(db).used(h, "jev", "2026-09-24") == 97


def test_utc_day_rollover(proxy):
    tok = proxy.mint()
    proxy.clock.now = datetime(2026, 9, 24, 23, 59, 59, tzinfo=timezone.utc)
    for _ in range(5):
        assert proxy.request("POST", "/v1/jev", jev_body(), token=tok)[0] == 200
    s, d, _, h = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 429 and d["resets_at"] == "2026-09-25T00:00:00Z" and h["Retry-After"] == "1"
    proxy.clock.now = datetime(2026, 9, 25, 0, 0, 0, tzinfo=timezone.utc)
    s, _, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 200
    s, d, _, _ = proxy.request("GET", "/v1/usage", token=tok)
    assert d["used_today"] == 1 and d["resets_at"] == "2026-09-26T00:00:00Z"
    # Year boundary, since date arithmetic is where these go wrong.
    proxy.clock.now = datetime(2026, 12, 31, 18, 0, tzinfo=timezone.utc)
    assert proxy.request("GET", "/v1/usage", token=tok)[1]["resets_at"] == "2027-01-01T00:00:00Z"


# ------------------------------------------------------------------ upstream failure

def test_upstream_500_is_generic_502_refunded_and_never_echoes_the_key(proxy):
    tok = proxy.mint()
    proxy.upstream.mode = "error500"
    s, d, raw, h = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 502 and d == {"error": "upstream_error"}
    assert_no_leak(raw.decode() + json.dumps(h), [])
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 0
    # Same through the Gemini route, whose key is a different one.
    s, d, raw, h = proxy.request("POST", GEM, gem_body(), token=tok)
    assert s == 502 and d == {"error": "upstream_error"}
    assert_no_leak(raw.decode() + json.dumps(h), [])


def test_upstream_rejecting_our_key_is_502_not_401(proxy):
    """If the client saw 401 it would think its own device token was revoked."""
    tok = proxy.mint()
    proxy.upstream.mode = "error401"
    s, d, raw, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 502 and d == {"error": "upstream_error"}
    assert FAKE_JEV_KEY.encode() not in raw


def test_upstream_400_is_client_error_without_upstream_text(proxy):
    tok = proxy.mint()
    proxy.upstream.mode = "bad400"
    s, d, raw, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 400 and d == {"error": "upstream_rejected_request"}
    assert FAKE_JEV_KEY.encode() not in raw


def test_upstream_busy_is_503_retryable_and_refunded(proxy):
    tok = proxy.mint()
    proxy.upstream.mode = "busy429"
    s, d, _, h = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 503 and d == {"error": "upstream_busy"} and h["Retry-After"] == "1"
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 0


def test_upstream_timeout_is_504_and_not_refunded(proxy):
    tok = proxy.mint()
    proxy.upstream.mode = "hang"                     # 3s against a 1s upstream timeout
    s, d, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 504 and d == {"error": "upstream_timeout"}
    # Jev may have done and billed the work, so the unit stays spent.
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 1


def test_upstream_unreachable_is_502_and_refunded(proxy):
    tok = proxy.mint()
    s0 = socket.socket()
    s0.bind(("127.0.0.1", 0))
    dead = s0.getsockname()[1]
    s0.close()                                       # a port with nothing listening
    from proxy.app import UpstreamPool
    proxy.srv.jev_pool = UpstreamPool(f"http://127.0.0.1:{dead}/v1/systemone", 1.0)
    s, d, _, _ = proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    assert s == 502 and d == {"error": "upstream_error"}
    assert proxy.store.used(hash_token(tok), "jev", "2026-09-24") == 0


def test_stale_pooled_upstream_connection_is_retried_once(proxy):
    tok = proxy.mint()
    assert proxy.request("POST", "/v1/jev", jev_body(), token=tok)[0] == 200
    # Kill the pooled keep-alive socket under the proxy, as an idle upstream would.
    conn = proxy.srv.jev_pool._idle.get_nowait()
    conn.sock.shutdown(socket.SHUT_RDWR)
    proxy.srv.jev_pool._idle.put_nowait(conn)
    assert proxy.request("POST", "/v1/jev", jev_body(), token=tok)[0] == 200


# ------------------------------------------------------------------ gemini

def test_gemini_path_forwards_with_gemini_key_and_clamps_output(proxy):
    tok = proxy.mint()
    body = gem_body()
    body["generationConfig"] = {"maxOutputTokens": 100000, "temperature": 0.1}
    s, d, _, _ = proxy.request("POST", GEM, body, token=tok)
    assert s == 200 and d == GEMINI_ANSWER
    hit = proxy.upstream.hits[0]
    assert hit["path"] == "/v1beta/models/gemini-3.5-flash-lite:generateContent"
    assert hit["headers"]["X-goog-api-key"] == FAKE_GEMINI_KEY
    assert "Authorization" not in hit["headers"]
    sent = json.loads(hit["body"])
    assert sent["contents"] == body["contents"]
    assert sent["generationConfig"] == {"maxOutputTokens": 1024, "temperature": 0.1}


def test_gemini_rejects_unlisted_models_and_billable_extras(proxy):
    tok = proxy.mint()
    assert proxy.request("POST", "/v1/gemini/gemini-9-ultra:generateContent",
                         gem_body(), token=tok)[1] == {"error": "model_not_allowed"}
    assert proxy.request("POST", "/v1/gemini/../../x:generateContent",
                         gem_body(), token=tok)[0] == 404
    for extra in ({"tools": [{"googleSearch": {}}]}, {"cachedContent": "c/1"}):
        s, _, _, _ = proxy.request("POST", GEM, {**gem_body(), **extra}, token=tok)
        assert s == 400
    assert proxy.request("POST", GEM, {"contents": "nope"}, token=tok)[0] == 400
    assert proxy.upstream.hits == []


def test_gemini_has_its_own_cap_separate_from_jev(proxy):
    tok = proxy.mint()                               # free: 5 jev, 3 gemini
    for _ in range(3):
        assert proxy.request("POST", GEM, gem_body(), token=tok)[0] == 200
    s, d, _, _ = proxy.request("POST", GEM, gem_body(), token=tok)
    assert s == 429 and d["error"] == "daily_limit" and d["limit"] == 3
    # Screen descriptions used up; voice requests still work.
    assert proxy.request("POST", "/v1/jev", jev_body(), token=tok)[0] == 200
    s, d, _, _ = proxy.request("GET", "/v1/usage", token=tok)
    assert d["used_today"] == 1 and d["gemini"] == {"used_today": 3, "limit_today": 3}


def test_gemini_not_configured_is_503(proxy):
    tok = proxy.mint()
    proxy.cfg.gemini_key = None
    s, d, _, _ = proxy.request("POST", GEM, gem_body(), token=tok)
    assert s == 503 and d == {"error": "not_configured"}


def test_gemini_accepts_a_large_screenshot_but_not_past_its_limit(proxy):
    tok = proxy.mint()
    ok = gem_body("SCREEN-PIXELS-" + "A" * (400 * 1024))
    assert proxy.request("POST", GEM, ok, token=tok)[0] == 200
    big = gem_body("SCREEN-PIXELS-" + "A" * (600 * 1024))
    assert proxy.request("POST", GEM, big, token=tok)[0] == 413


# ------------------------------------------------------------------ usage

def test_usage_endpoint_shape(proxy):
    tok = proxy.mint()
    proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    s, d, _, _ = proxy.request("GET", "/v1/usage?x=QUERY-SECRET", token=tok)
    assert s == 200
    assert d == {"plan": "free", "used_today": 2, "limit_today": 5,
                 "resets_at": "2026-09-25T00:00:00Z", "scope": "day",
                 "gemini": {"used_today": 0, "limit_today": 3}}


# ------------------------------------------------------------------ logs

def test_log_lines_carry_route_status_and_hash_prefix_only(proxy, log_buffer):
    start = log_buffer.tell()
    tok = proxy.mint()
    proxy.request("POST", "/v1/jev?key=QUERY-SECRET", jev_body(), token=tok)
    proxy.request("POST", GEM, gem_body(), token=tok)
    proxy.request("POST", "/v1/jev", b"{SAID-OUT-LOUD broken json", token=tok)
    proxy.upstream.mode = "error500"
    proxy.request("POST", "/v1/jev", jev_body(), token=tok)
    proxy.request("POST", "/v1/jev", jev_body(), token="mmp_SAID-OUT-LOUD-guess")
    # A raw protocol violation, which the stock server would log verbatim.
    with socket.create_connection(("127.0.0.1", proxy.port)) as raw:
        raw.sendall(b"GARBAGE SAID-OUT-LOUD QUERY-SECRET\r\n\r\n")
        raw.recv(4096)
    logs = log_buffer.getvalue()[start:]
    lines = [ln for ln in logs.splitlines() if ln.strip()]
    assert any(ln.startswith("POST /v1/jev 200 ") and f"tok={hash_token(tok)[:8]}" in ln
               for ln in lines), logs
    assert any("/v1/gemini/gemini-3.5-flash-lite 200" in ln for ln in lines)
    assert any("POST /v1/jev 400" in ln for ln in lines)
    assert any("POST /v1/jev 502" in ln for ln in lines)
    assert any("upstream jev status 500" in ln for ln in lines)
    assert any("POST /v1/jev 401" in ln and "tok=-" in ln for ln in lines)
    assert any("protocol error" in ln for ln in lines)
    assert_no_leak(logs, [tok])


# ------------------------------------------------------------------ admin + storage

def test_admin_mint_list_revoke(tmp_path, capsys):
    db = str(tmp_path / "a.db")
    assert admin.main(["--db", db, "mint", "--plan", "free", "--label", "laptop"]) == 0
    out, err = capsys.readouterr()
    token = out.strip()
    assert token.startswith("mmp_") and len(token) > 40
    assert "only time" in err and token not in err
    assert admin.main(["--db", db, "list"]) == 0
    out, _ = capsys.readouterr()
    assert token not in out and hash_token(token)[:12] in out and "active" in out
    # The database holds the hash and never the token.
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("a.db"))
    assert token.encode() not in raw and hash_token(token).encode() in raw
    assert admin.main(["--db", db, "revoke", "abc"]) == 1          # too short
    assert admin.main(["--db", db, "revoke", hash_token(token)[:10]]) == 0
    assert Store(db).plan_for(token) is None
    assert admin.main(["--db", db, "revoke", hash_token(token)[:10]]) == 1  # already


def test_admin_revoke_by_token_on_stdin_and_write_credentials(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "b.db")
    cred = tmp_path / "state" / "credentials.json"
    assert admin.main(["--db", db, "mint", "--plan", "pro", "--write-credentials",
                       str(cred), "--proxy-url", "http://127.0.0.1:8810"]) == 0
    out, _ = capsys.readouterr()
    d = json.loads(cred.read_text())
    assert d["proxy_url"] == "http://127.0.0.1:8810" and d["token"] not in out
    assert oct(cred.stat().st_mode & 0o777) == "0o600"
    assert Store(db).plan_for(d["token"])[1] == "pro"
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(d["token"] + "\n"))
    assert admin.main(["--db", db, "revoke", "--token-stdin"]) == 0
    assert Store(db).plan_for(d["token"]) is None


def test_the_leak_scanner_itself_catches_a_leak():
    """The per-test leak scan is only evidence if it can fail."""
    import pytest
    for s in (FAKE_JEV_KEY, FAKE_GEMINI_KEY, "SAID-OUT-LOUD", "SCREEN-PIXELS"):
        with pytest.raises(AssertionError):
            assert_no_leak(f"x {s} y", [])
    with pytest.raises(AssertionError):
        assert_no_leak("tok mmp_abc", ["mmp_abc"])

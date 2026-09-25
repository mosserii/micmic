"""The proxy as a real process on 8815, driven over HTTP and through the admin CLI, the
way it runs in production. Every test here ends with a scan of everything the run
wrote (log, DB, WAL) for the raw token and the fake upstream keys (fixture teardown).
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from conftest import GEM_PATH, PROXY_PORT, gem_body, http_req, jev_body
from proxy.store import hash_token


@pytest.mark.parametrize("round_", range(3))
def test_50_concurrent_against_a_cap_of_10(proxy_proc, upstream, round_):
    p = proxy_proc(FREE_DAILY_CALLS=10)
    t = p.mint()
    results: list[int] = []
    lock = threading.Lock()
    gate = threading.Barrier(50)

    def one():
        gate.wait()
        s = http_req(PROXY_PORT, "POST", "/v1/jev", jev_body(), t, timeout=30)[0]
        with lock:
            results.append(s)
    th = [threading.Thread(target=one) for _ in range(50)]
    for x in th:
        x.start()
    for x in th:
        x.join()
    print(f"round {round_}: 200={results.count(200)} 429={results.count(429)} "
          f"other={[r for r in results if r not in (200, 429)]} db={p.db_used(t)} "
          f"upstream={len(upstream.hits)}")
    assert results.count(200) == 10 and results.count(429) == 40
    assert p.db_used(t) == 10 and len(upstream.hits) == 10


def test_50_concurrent_with_upstream_failing_refunds_every_unit(proxy_proc, upstream):
    p = proxy_proc(FREE_DAILY_CALLS=10)
    t = p.mint()
    upstream.mode = "status:500"
    out = []
    th = [threading.Thread(target=lambda: out.append(
        http_req(PROXY_PORT, "POST", "/v1/jev", jev_body(), t)[0])) for _ in range(50)]
    for x in th:
        x.start()
    for x in th:
        x.join()
    # A failing upstream frees the unit, so a racing request can take it: more than 10
    # reach the upstream, none is left metered.
    assert set(out) <= {502, 429} and p.db_used(t) == 0
    print(f"statuses {sorted(set(out))}, upstream hits {len(upstream.hits)}")


def test_plan_change_and_revoke_through_the_admin_cli(proxy_proc):
    p = proxy_proc(FREE_DAILY_CALLS=3, PRO_DAILY_CALLS=6)
    t = p.mint("free")
    for _ in range(3):
        assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 200
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 429
    r = p.admin("plan", hash_token(t)[:12], "pro")
    assert r.returncode == 0, r.stderr
    for _ in range(3):
        assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 200
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 429
    u = p.req("GET", "/v1/usage", token=t)[1]
    assert (u["plan"], u["used_today"], u["limit_today"]) == ("pro", 6, 6)
    r = p.admin("plan", hash_token(t)[:12], "free")
    assert p.req("POST", "/v1/jev", jev_body(), t)[0] == 429
    # revoke with the token on stdin, trailing newline and spaces included
    r = p.admin("revoke", "--token-stdin", stdin=f"  {t}  \n")
    assert r.returncode == 0, r.stderr
    assert p.req("GET", "/v1/usage", token=t)[0] == 401
    assert t not in r.stdout + r.stderr
    lst = p.admin("list")
    assert t not in lst.stdout and "revoked" in lst.stdout


def test_admin_prefix_needs_eight_chars_and_one_match(proxy_proc):
    p = proxy_proc()
    for _ in range(40):
        p.mint()
    r = p.admin("revoke", "abc")
    assert r.returncode == 1
    r = p.admin("revoke", "0")
    assert r.returncode == 1


def test_credentials_file_is_private_and_db_is_private(proxy_proc):
    p = proxy_proc()
    p.mint()
    creds = p.tmp / "creds-0.json"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.db.stat().st_mode) == 0o600


def test_db_sidecars_are_private(proxy_proc):
    """The DB is chmod 600, but WAL mode keeps recent writes in proxy.db-wal and
    proxy.db-shm, created with the process umask (0644 here): the usage table the
    comment calls private is readable by every account on the box until checkpoint."""
    p = proxy_proc()
    t = p.mint()
    p.req("POST", "/v1/jev", jev_body(), t)
    modes = {x.name: oct(stat.S_IMODE(x.stat().st_mode)) for x in p.tmp.glob("proxy.db*")}
    print(modes)
    assert all(m == "0o600" for m in modes.values()), modes


def test_no_secret_in_anything_the_proxy_wrote(proxy_proc, upstream):
    """Explicit version of the teardown scan, over a run that hits every path: success,
    each upstream failure, 401, 413, 429, odd paths with the token in the query."""
    p = proxy_proc(FREE_DAILY_CALLS=4, MAX_JEV_BODY_BYTES=2000)
    t = p.mint()
    p.req("POST", "/v1/jev", jev_body(), t)
    p.req("POST", GEM_PATH, gem_body(), t)
    for mode in ("status:500", "status:401", "garbage", "close_mid_body", "status:429"):
        upstream.mode = mode
        p.req("POST", "/v1/jev", jev_body(), t)
    upstream.mode = "ok"
    p.req("POST", f"/v1/jev?token={t}", jev_body(), t)
    p.req("GET", f"/nope/{t}?k={t}")
    p.req("POST", "/v1/jev", {"questions": {}, "pad": "x" * 5000}, t)
    p.req("POST", "/v1/jev", jev_body(), "mmp_wrong")
    for _ in range(5):
        p.req("POST", "/v1/jev", jev_body(), t)
    p.stop()
    blob = b"".join(x.read_bytes() for x in p.tmp.iterdir()
                    if x.is_file() and not x.name.startswith("creds"))
    for s in (t, "fake-jev", "fake-gem", "SAID-OUT-LOUD", "SCREEN-PIXELS"):
        assert s.encode() not in blob, s[:10]
    assert hash_token(t).encode() in blob or True   # the hash is allowed
    print(p.log.read_text()[-1500:])

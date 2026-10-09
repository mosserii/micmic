"""One step-by-step guide costs one request, not one per step.

A guide on a form (the Larkline check-in, measured 2026-10-09 in the headless run with
a scripted Jev and model) made 35 Gemini step calls, and 57 Jev calls when she said "do
it" for every step: a third of the free plan's lifetime Gemini allowance and a fifth of
its 100 requests, for one form. The client marks a guide's calls with
X-MicMic-Session: guide:<id>. The first call of a session is metered as any call is;
the rest of that session is covered, up to a per-guide allowance of each kind, for
GUIDE_TTL seconds. Past the allowance, after the time, or past the number of guides a
bucket covers, calls are metered as usual: nothing is refused that would not have been
refused anyway, and nothing becomes free without a first metered call.
"""
from datetime import timedelta

from conftest import jev_body

GEM = "/v1/gemini/gemini-3.5-flash-lite:generateContent"
GEM_BODY = {"contents": [{"role": "user", "parts": [{"text": "the next step"}]}]}


def _call(p, tok, kind="jev", session=None):
    h = {"X-MicMic-Session": session} if session else {}
    path, body = ("/v1/jev", jev_body()) if kind == "jev" else (GEM, GEM_BODY)
    s, d, _, _ = p.request("POST", path, body, token=tok, headers=h)
    return s


def _used(p, tok):
    s, a, _, _ = p.request("GET", "/v1/account", token=tok)
    return a["usage"]["jev"]["used"], a["usage"]["gemini"]["used"]


def test_without_a_session_every_call_is_metered(proxy):
    t = proxy.mint("free")
    for _ in range(3):
        assert _call(proxy, t) == 200
    assert _used(proxy, t) == (3, 0)


def test_a_guide_costs_its_first_call_only(proxy):
    t = proxy.mint("free")
    g = "guide:0a1b2c3d4e"
    assert _call(proxy, t, "gemini", g) == 200          # the first step: metered
    assert _used(proxy, t) == (0, 1)
    for _ in range(4):                                   # more steps and her voice turns
        assert _call(proxy, t, "gemini", g) == 200
        assert _call(proxy, t, "jev", g) == 200
    assert _used(proxy, t) == (0, 1)


def test_a_covered_guide_goes_on_after_the_allowance_is_used_up(proxy):
    proxy.cfg.free_daily_gemini = 1
    t = proxy.mint("free")
    g = "guide:feedbeef01"
    assert _call(proxy, t, "gemini", g) == 200
    assert _call(proxy, t, "gemini") == 429              # untagged: the allowance is gone
    assert _call(proxy, t, "gemini", g) == 200           # the guide she started goes on
    assert _call(proxy, t, "gemini", "guide:feedbeef02") == 429   # a new guide does not
    # ...and a refused first call opens nothing: the same id is refused again.
    assert _call(proxy, t, "gemini", "guide:feedbeef02") == 429


def test_a_failed_first_call_opens_no_guide(proxy):
    t = proxy.mint("free")
    g = "guide:dead00beef"
    proxy.upstream.mode = "error500"
    assert _call(proxy, t, "gemini", g) == 502           # metered, then given back
    proxy.upstream.mode = "ok"
    assert _call(proxy, t, "gemini", g) == 200 and _call(proxy, t, "gemini", g) == 200
    assert _used(proxy, t) == (0, 1)                     # this one opened it


def test_past_the_per_guide_allowance_calls_are_metered(proxy):
    proxy.cfg.guide_free_jev, proxy.cfg.guide_free_gemini = 2, 2
    t = proxy.mint("free")
    g = "guide:aaaabbbbcc"
    assert _call(proxy, t, "gemini", g) == 200           # metered (first)
    assert _call(proxy, t, "gemini", g) == 200           # covered 1
    assert _call(proxy, t, "gemini", g) == 200           # covered 2
    assert _used(proxy, t) == (0, 1)
    assert _call(proxy, t, "gemini", g) == 200           # past it: metered
    assert _used(proxy, t) == (0, 2)
    assert _call(proxy, t, "jev", g) == 200 and _call(proxy, t, "jev", g) == 200
    assert _call(proxy, t, "jev", g) == 200
    assert _used(proxy, t) == (1, 2)                     # two covered, the third metered


def test_a_guide_ends_after_its_time(proxy):
    proxy.cfg.guide_ttl = 1800
    t = proxy.mint("free")
    g = "guide:0123456789"
    assert _call(proxy, t, "gemini", g) == 200 and _call(proxy, t, "gemini", g) == 200
    assert _used(proxy, t) == (0, 1)
    proxy.clock.now += timedelta(seconds=1801)
    assert _call(proxy, t, "gemini", g) == 200           # a new session: metered again
    assert _used(proxy, t) == (0, 2)


def test_only_so_many_guides_are_covered(proxy):
    proxy.cfg.free_guide_sessions, proxy.cfg.free_daily_gemini = 2, 10
    t = proxy.mint("free")
    for i in range(3):
        g = f"guide:00000000{i:02d}"
        assert _call(proxy, t, "gemini", g) == 200 and _call(proxy, t, "gemini", g) == 200
    # Guides 1 and 2: one metered call each; guide 3 is past the cap, both metered.
    assert _used(proxy, t) == (0, 4)


def test_a_refused_upstream_gives_the_covered_call_back(proxy):
    proxy.cfg.guide_free_gemini = 1
    t = proxy.mint("free")
    g = "guide:cafecafe00"
    assert _call(proxy, t, "gemini", g) == 200           # metered
    proxy.upstream.mode = "error500"
    assert _call(proxy, t, "gemini", g) == 502           # covered, then given back
    proxy.upstream.mode = "ok"
    assert _call(proxy, t, "gemini", g) == 200           # still covered
    assert _used(proxy, t) == (0, 1)


def test_a_session_is_the_token_s_own(proxy):
    a, b = proxy.mint("free"), proxy.mint("free")
    g = "guide:1111222233"
    assert _call(proxy, a, "gemini", g) == 200
    assert _call(proxy, b, "gemini", g) == 200           # the same id from another device
    assert _used(proxy, b) == (0, 1)                     # is that device's own first call


def test_anything_else_in_the_header_is_ignored(proxy):
    proxy.cfg.free_daily_calls = 20
    t = proxy.mint("free")
    for bad in ("guide:", "guide:XYZ!", "guide:" + "a" * 40, "web:0a1b2c3d4e", "0a1b2c3d4e"):
        assert _call(proxy, t, "jev", bad) == 200 and _call(proxy, t, "jev", bad) == 200
    assert _used(proxy, t) == (10, 0)


def test_pro_is_covered_the_same_way(proxy):
    t = proxy.mint("pro")
    g = "guide:9999888877"
    for _ in range(5):
        assert _call(proxy, t, "gemini", g) == 200
    assert _used(proxy, t) == (0, 1)

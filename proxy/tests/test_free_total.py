"""The free plan is 100 requests to try, in total: the usage never rolls over at
midnight, the limit says so (scope "total", no resets_at), and Pro stays daily."""
from datetime import timedelta

from conftest import jev_body


def _jev(p, tok):
    s, d, _, _ = p.request("POST", "/v1/jev", jev_body(), token=tok)
    return s, d


def test_free_is_a_total_that_never_resets(proxy):
    proxy.cfg.free_scope, proxy.cfg.free_daily_calls = "total", 3
    t = proxy.mint("free")
    for _ in range(3):
        assert _jev(proxy, t)[0] == 200
    s, d = _jev(proxy, t)
    assert s == 429 and d["scope"] == "total" and d["resets_at"] is None and d["limit"] == 3
    proxy.clock.now += timedelta(days=2)          # two midnights later: still used up
    s, d = _jev(proxy, t)
    assert s == 429 and d["scope"] == "total"
    s, a, _, _ = proxy.request("GET", "/v1/account", token=t)
    assert a["scope"] == "total" and a["resets_at"] is None
    assert a["usage"]["jev"] == {"used": 3, "limit": 3}


def test_pro_stays_daily_and_rolls_over(proxy):
    proxy.cfg.free_scope, proxy.cfg.pro_daily_calls = "total", 2
    t = proxy.mint("pro")
    assert _jev(proxy, t)[0] == 200 and _jev(proxy, t)[0] == 200
    s, d = _jev(proxy, t)
    assert s == 429 and d["scope"] == "day" and d["resets_at"]
    proxy.clock.now += timedelta(days=1)
    assert _jev(proxy, t)[0] == 200


def test_day_scope_still_available(proxy):
    proxy.cfg.free_scope, proxy.cfg.free_daily_calls = "day", 1
    t = proxy.mint("free")
    assert _jev(proxy, t)[0] == 200
    s, d = _jev(proxy, t)
    assert s == 429 and d["scope"] == "day" and d["resets_at"]

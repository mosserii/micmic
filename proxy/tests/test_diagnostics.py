"""POST /v1/diagnostics: the phone's turn log, sent only while Share diagnostics is on.

Text and timings, never audio; stored under the device id for 30 days; readable only
with `admin diagnostics`; never logged (the `proxy` fixture's leak scan covers every test
here, and every item below carries the SAID-OUT-LOUD marker)."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from conftest import ios_signup
from proxy import admin
from proxy.store import hash_token


def turn(n: int = 0, **over) -> dict:
    t = {"v": 1, "kind": "turn", "at": "2026-10-04T18:30:00+03:00", "activation": "orb_tap",
         "mic": {"permission": "granted", "route": "MicrophoneBuiltIn"},
         "input": {"peak": 0.41, "rms": 0.07, "speech_ms": 1200, "duration_ms": 2600},
         "recognizers": [{"engine": "apple_analyzer", "final": f"SAID-OUT-LOUD text Dana {n}",
                          "final_ms": 2700},
                         {"engine": "parakeet", "final": f"SAID-OUT-LOUD text Dana {n}",
                          "ms": 180}],
         "end": "silence",
         "sent": {"text": f"SAID-OUT-LOUD text Dana {n}"},
         "brain": {"turn_id": f"t-{n}", "status": 200, "did": "message_draft",
                   "card": "message", "server_ms": 410, "round_trip_ms": 640},
         "marks": {"first_partial": 300, "final": 2700, "sent": 2710, "reply": 3350,
                   "first_audio": 3900, "spoken": 6200, "done": 6250},
         "tts": {"path": "cloud", "first_audio_ms": 550}}
    t.update(over)
    return t


def post(p, token, items, **body_over):
    body = {"v": 1, "app_version": "1.0.0", "build": "5", "items": items}
    body.update(body_over)
    s, d, _, h = p.request("POST", "/v1/diagnostics", body, token=token)
    return s, d, h


def device_of(p, token) -> str:
    return p.store.account(hash_token(token))["device_id"]


def test_needs_a_device_token(proxy):
    s, d, _, _ = proxy.request("POST", "/v1/diagnostics", {"items": [turn()]})
    assert s == 401 and d == {"error": "unauthorized"}
    s, d, _, _ = proxy.request("POST", "/v1/diagnostics", {"items": [turn()]},
                               token="mmp_not-a-real-token-at-all-000000000000")
    assert s == 401


def test_items_are_stored_under_the_device_and_read_back(proxy):
    s, d = ios_signup(proxy)
    assert s == 201
    tok = d["token"]
    s, out, _ = post(proxy, tok, [turn(1), turn(2)])
    assert s == 200 and out == {"stored": 2, "max_items": 50, "retention_days": 30}
    rows = proxy.store.diagnostics_for(d["device_id"])
    assert [json.loads(r["body"])["brain"]["turn_id"] for r in rows] == ["t-1", "t-2"]
    assert rows[0]["client_at"] == "2026-10-04T18:30:00+03:00"
    assert rows[0]["kind"] == "turn" and rows[0]["app_version"] == "1.0.0"
    # Another device sees none of them.
    s2, d2 = ios_signup(proxy)
    assert proxy.store.diagnostics_for(d2["device_id"]) == []


def test_audio_is_refused_anywhere_in_an_item(proxy):
    tok = proxy.mint()
    for bad in ({"audio": "AAAA"}, {"input": {"samples": [0.1, 0.2]}},
                {"recognizers": [{"engine": "parakeet", "PCM": "AAAA"}]},
                {"x": [{"y": {"recording": "x"}}]}):
        s, d, _ = post(proxy, tok, [turn(**bad)])
        assert (s, d) == (400, {"error": "audio_not_allowed"}), bad
    assert proxy.store.diagnostics_count() == 0


def test_size_and_shape_limits(make_proxy):
    proxy = make_proxy(diag_per_minute=100)
    tok = proxy.mint()
    assert post(proxy, tok, [])[0] == 400
    assert post(proxy, tok, "nope")[0] == 400
    assert post(proxy, tok, [turn()] * 51)[:2] == (400, {"error": "bad_request", "max_items": 50})
    assert post(proxy, tok, [1, 2])[0] == 400
    s, d, _ = post(proxy, tok, [turn(errors=["x" * 17000])])
    assert s == 413 and d["error"] == "item_too_large"
    # The whole body past 64 KB is refused before it is read.
    s, d, _, _ = proxy.request("POST", "/v1/diagnostics",
                               b'{"items": ["' + b"x" * (70 * 1024) + b'"]}', token=tok)
    assert s == 413 and d["error"] == "body_too_large"
    s, d, _, _ = proxy.request("POST", "/v1/diagnostics", b"{not json", token=tok)
    assert s == 400 and d["error"] == "bad_json"
    assert proxy.store.diagnostics_count() == 0


def test_a_bad_client_time_or_kind_is_kept_out_of_the_columns(proxy):
    tok = proxy.mint()
    s, _, _ = post(proxy, tok, [turn(at="yesterday", kind="DROP TABLE")])
    assert s == 200
    r = proxy.store.diagnostics_for(device_of(proxy, tok))[0]
    assert r["client_at"] is None and r["kind"] == "turn"


def test_uploads_per_minute_are_limited(make_proxy):
    p = make_proxy(diag_per_minute=2)
    tok = p.mint()
    assert post(p, tok, [turn()])[0] == 200
    assert post(p, tok, [turn()])[0] == 200
    s, d, h = post(p, tok, [turn()])
    assert s == 429 and d["error"] == "rate_limited" and h.get("Retry-After") == "60"
    # Another device is not held back by this one.
    assert post(p, p.mint(), [turn()])[0] == 200


def test_items_per_day_are_limited_and_reset_at_midnight(make_proxy):
    p = make_proxy(diag_daily_items=3, diag_per_minute=100)
    tok = p.mint()
    assert post(p, tok, [turn(), turn()])[0] == 200
    s, d, h = post(p, tok, [turn(), turn()])
    assert s == 429 and d["error"] == "diagnostics_limit" and d["limit"] == 3
    assert int(h["Retry-After"]) > 0
    assert post(p, tok, [turn()])[0] == 200            # exactly the one left
    p.clock.now += timedelta(days=1)
    assert post(p, tok, [turn(), turn()])[0] == 200
    assert p.store.diagnostics_count(device_of(p, tok)) == 5


def test_kill_switch(make_proxy):
    p = make_proxy(diagnostics_enabled=False)
    s, d, h = post(p, p.mint(), [turn()])
    assert s == 503 and d == {"error": "diagnostics_disabled"} and h.get("Retry-After")
    assert p.store.diagnostics_count() == 0


def test_rows_older_than_the_retention_are_purged(make_proxy):
    p = make_proxy(diag_per_minute=100)
    tok = p.mint()
    dev = device_of(p, tok)
    assert post(p, tok, [turn(1)])[0] == 200
    p.clock.now += timedelta(days=29)
    assert post(p, tok, [turn(2)])[0] == 200
    assert p.store.diagnostics_count(dev) == 2
    p.clock.now += timedelta(days=2)                   # the first is now 31 days old
    assert post(p, tok, [turn(3)])[0] == 200
    left = [json.loads(r["body"])["brain"]["turn_id"] for r in p.store.diagnostics_for(dev)]
    assert left == ["t-2", "t-3"]
    # The server's own purge (start, hourly) does it without any upload.
    p.clock.now += timedelta(days=31)
    assert p.srv.purge_diagnostics() == 2
    assert p.store.diagnostics_count(dev) == 0


def test_a_device_keeps_at_most_its_newest_rows(make_proxy):
    p = make_proxy(diag_max_rows_per_device=3, diag_per_minute=100)
    tok = p.mint()
    for i in range(5):
        assert post(p, tok, [turn(i)])[0] == 200
    left = [json.loads(r["body"])["brain"]["turn_id"]
            for r in p.store.diagnostics_for(device_of(p, tok))]
    assert left == ["t-2", "t-3", "t-4"]


def test_account_deletion_deletes_the_diagnostics(proxy):
    tok = proxy.mint()                                  # a Mac device: no brain to ask
    dev = device_of(proxy, tok)
    other = proxy.mint()
    assert post(proxy, tok, [turn(1)])[0] == 200
    assert post(proxy, other, [turn(2)])[0] == 200
    s, d, _, _ = proxy.request("POST", "/v1/account/delete", {"confirm": True}, token=tok)
    assert s == 200 and d["deleted"] is True
    assert proxy.store.diagnostics_count(dev) == 0
    assert proxy.store.diagnostics_count(device_of(proxy, other)) == 1


def test_admin_reads_a_device_by_its_id_prefix(tmp_path, capsys):
    """The owner's view. Not through the `proxy` fixture: printing what was said is the
    point of this command, and that fixture fails any test whose output carries it."""
    from proxy.store import Store
    db = tmp_path / "proxy.db"
    store = Store(db)
    tok = store.mint("free")
    dev = store.account(hash_token(tok))["device_id"]
    items = [turn(1), turn(2, sent={"reason": "empty"}, brain={}, errors=["no_text"]),
             # Stopped on the phone (AI services off): the words are held, never "sent".
             turn(3, sent={"reason": "consent", "held": "SAID-OUT-LOUD text Dana 3"}, brain={}),
             # An older build kept the words AND a reason: still NOT SENT.
             turn(4, sent={"reason": "superseded", "text": "SAID-OUT-LOUD text Dana 4"})]
    store.add_diagnostics(dev, [{"client_at": None, "kind": "turn",
                                 "body": json.dumps(i)} for i in items], 1_000, 100)
    capsys.readouterr()
    assert admin.main(["--db", str(db), "diagnostics", dev[:9]]) == 0
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    by = {n: next(ln for ln in lines if f'Dana {n}"' in ln) for n in (1, 2, 3, 4)}
    assert len(lines) == 4 and "4 item(s) for " + dev in err
    assert 'sent="SAID-OUT-LOUD text Dana 1"' in by[1] and "did=message_draft" in by[1]
    assert "rt=640 server=410" in by[1]
    assert "ms first_partial=300 final=2700 sent=2710 reply=3350 first_audio=3900 spoken=6200" in by[1]
    assert "NOT SENT (empty)" in by[2] and "no_text" in by[2]
    assert "NOT SENT (consent)" in by[3] and 'held="SAID-OUT-LOUD text Dana 3"' in by[3]
    assert 'sent="' not in by[3]
    assert "NOT SENT (superseded)" in by[4] and 'sent="' not in by[4]
    assert 'held="SAID-OUT-LOUD text Dana 4"' in by[4]
    assert admin.main(["--db", str(db), "diagnostics", dev, "--json", "--limit", "4"]) == 0
    out, _ = capsys.readouterr()
    rows = [json.loads(x) for x in out.strip().splitlines()]
    empty = next(r for r in rows if r["item"]["sent"] == {"reason": "empty"})
    assert empty["item"]["brain"] == {}
    assert admin.main(["--db", str(db), "diagnostics", hash_token(tok)[:10]]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 4
    assert admin.main(["--db", str(db), "diagnostics", "dev_nomatch"]) == 1
    assert admin.main(["--db", str(db), "diagnostics-purge"]) == 0
    assert "purged 4" in capsys.readouterr().out          # stored at t=1000: long ago
    assert store.diagnostics_count(dev) == 0


def test_admin_shows_the_talk_button_fields_and_keeps_unknown_ones(tmp_path, capsys):
    """S9 (build 6): how the press went, the listener's new marks, and any field a newer
    build adds are on the owner's line; --json keeps the item exactly as stored."""
    from proxy.store import Store
    db = tmp_path / "proxy.db"
    store = Store(db)
    tok = store.mint("free")
    dev = store.account(hash_token(tok))["device_id"]
    item = turn(7, activation="orb_hold_to_tap", press_ms=180, handler_lag_ms=420,
                cancel_by="own_playback", own_playback=True, final_rule="parakeet_longer",
                marks={"mic_open": 40, "first_buffer": 95, "first_partial": 300, "end": 2600,
                       "drain_ms": 120, "finalize_ms": 310, "final": 2700, "sent": 2710,
                       "engine_restart": 1, "aec": True, "final_deadline_hit": False,
                       "model_warm": True, "transcribing": 2650, "built": "6"},
                listener={"route": "MicrophoneBuiltIn", "vad": "silero"},
                brand_new_field=3)
    store.add_diagnostics(dev, [{"client_at": None, "kind": "turn", "body": json.dumps(item)}],
                          1_000, 100)
    capsys.readouterr()
    assert admin.main(["--db", str(db), "diagnostics", dev]) == 0
    line = capsys.readouterr().out.strip()
    assert "orb_hold_to_tap" in line
    assert ("press_ms=180 handler_lag_ms=420 cancel_by=own_playback own_playback=True "
            "final_rule=parakeet_longer") in line
    assert ("ms mic_open=40 first_buffer=95 first_partial=300 end=2600 drain_ms=120 "
            "finalize_ms=310 final=2700 sent=2710 engine_restart=1 aec=True "
            "final_deadline_hit=False model_warm=True transcribing=2650 built=6") in line
    assert "brand_new_field=3" in line and '"vad":"silero"' in line
    assert admin.main(["--db", str(db), "diagnostics", dev, "--json"]) == 0
    row = json.loads(capsys.readouterr().out.strip())
    assert row["item"] == item


def test_nothing_of_an_item_reaches_the_log(proxy, log_buffer):
    start = log_buffer.tell()
    tok = proxy.mint()
    assert post(proxy, tok, [turn(7)])[0] == 200
    text = log_buffer.getvalue()[start:]
    assert "POST /v1/diagnostics 200" in text and "diagnostics stored 1 item(s)" in text
    assert "SAID-OUT-LOUD" not in text and "Dana" not in text


@pytest.mark.parametrize("path", ["/v1/diagnostics"])
def test_get_is_not_a_route(proxy, path):
    s, _, _, _ = proxy.request("GET", path, token=proxy.mint())
    assert s == 404

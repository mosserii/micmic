"""What she hears when the proxy path goes wrong, end to end:

    MicMic server (8814, MICMIC_MODE=proxy)  ->  proxy process (8815)  ->  fake upstream

Utterances are harmless (the time, a joke) and the fake Jev steers the router to the
clock answer, so nothing here can send, call or open anything. Rule under test: she
always hears a sentence, in the language she just spoke, never silence or a raw error.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from conftest import MICMIC_PORT, PROXY_PORT, http_req

HEBREW_PROFILE = {"setup_complete": True, "language": "hebrew", "name": "T",
                  "gender": "feminine", "speech_lang": "he-IL"}

UTTER = {"english": "what time is it", "hebrew": "מה השעה עכשיו",
         "russian": "который сейчас час", "arabic": "قديش الساعة هلأ"}


def _script_lang(s: str) -> str:
    for ch in s:
        o = ord(ch)
        if 0x0590 <= o <= 0x05FF:
            return "hebrew"
        if 0x0600 <= o <= 0x06FF:
            return "arabic"
        if 0x0400 <= o <= 0x04FF:
            return "russian"
    return "english"


def assert_heard(d: dict, lang: str) -> None:
    say = (d.get("say") or "").strip()
    assert say, f"silence: {d}"
    assert d.get("lang") == lang, f"answered in {d.get('lang')}, she spoke {lang}: {say}"
    assert _script_lang(say.replace("MicMic", "")) == lang, f"script of '{say}'"
    for raw in ("Traceback", "HTTP 5", "jev call failed", "RuntimeError", "Errno"):
        assert raw not in say, say
    assert "\u00ab" not in say and "|" not in say, f"raw gender marker reached her: {say}"


def _setup(tmp_path, proxy_proc, micmic, profile=HEBREW_PROFILE, **proxy_env):
    p = proxy_proc(**proxy_env)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    tok = p.mint(creds=state / "credentials.json")
    m = micmic(profile=profile)
    return p, m, tok


def test_harness_happy_path(tmp_path, proxy_proc, micmic):
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    d, _ = m.say(UTTER["english"])
    assert_heard(d, "english")
    assert m.health()["configured"] is True and p.db_used(tok) >= 1


@pytest.mark.parametrize("lang", ["english", "hebrew", "russian", "arabic"])
def test_daily_limit_is_said_in_her_language_with_her_clock(tmp_path, proxy_proc, micmic,
                                                            lang):
    p, m, tok = _setup(tmp_path, proxy_proc, micmic, FREE_DAILY_CALLS=0)
    d, took = m.say(UTTER[lang])
    assert d["did"] == "daily_limit", d
    assert_heard(d, lang)
    assert re.search(r"\d\d:\d\d", d["say"]), d["say"]
    assert d["detail"]["resets_at"].endswith("T00:00:00Z")
    assert took < 5


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_limit_reached_in_the_middle_of_a_request(tmp_path, proxy_proc, micmic, cap):
    p, m, tok = _setup(tmp_path, proxy_proc, micmic, FREE_DAILY_CALLS=cap)
    heard = []
    for _ in range(cap + 1):
        d, _ = m.say(UTTER["english"])
        heard.append(d["did"])
        assert_heard(d, "english")
    assert heard[-1] == "daily_limit", heard
    assert p.db_used(tok) == cap


def test_gemini_limit_is_said_in_her_language(tmp_path, proxy_proc, micmic, upstream):
    p, m, tok = _setup(tmp_path, proxy_proc, micmic, FREE_DAILY_GEMINI_CALLS=0)
    upstream.prefer, upstream.true_keys = ("chitchat",), ("is_complete",)
    d, _ = m.say("tell me a joke please")
    gem = [h for h in upstream.hits if "generateContent" in h["path"]]
    print(json.dumps({k: d.get(k) for k in ("did", "say", "lang")}, ensure_ascii=False),
          "gemini calls reached upstream:", len(gem))
    assert_heard(d, "english")


@pytest.mark.parametrize("round_", range(3))
def test_upstream_500_she_hears_her_own_language(tmp_path, proxy_proc, micmic, upstream,
                                                 round_):
    """FAILURE. Jev failing behind the proxy (502 to the client) surfaces as a
    RuntimeError, the server's catch-all answers with the PROFILE language (or Hebrew
    when there is none), and the line it picks is "huh": "I did not understand, say it
    again in different words", which blames her for an outage."""
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    upstream.mode = "status:500"
    d, took = m.say(UTTER["english"])
    print(round_, json.dumps({k: d.get(k) for k in ("did", "say", "lang")},
                             ensure_ascii=False), f"{took:.1f}s")
    assert p.db_used(tok) == 0
    assert_heard(d, "english")


@pytest.mark.parametrize("round_", range(3))
def test_fresh_install_proxy_error_is_not_in_hebrew(tmp_path, proxy_proc, micmic, upstream,
                                                    round_):
    """Guard (passes today). No profile yet: the catch-all would fall back to "hebrew",
    but onboarding records her language from the words before its first Jev call."""
    p, m, tok = _setup(tmp_path, proxy_proc, micmic, profile=None)
    upstream.mode = "status:500"
    d, _ = m.say(UTTER["english"])
    print(round_, json.dumps({k: d.get(k) for k in ("did", "say", "lang")},
                             ensure_ascii=False))
    assert_heard(d, "english")


@pytest.mark.parametrize("round_", range(3))
def test_outage_line_is_degendered(tmp_path, proxy_proc, micmic, upstream, round_):
    """FAILURE. The server's catch-all builds its line with router._sp() and never runs
    degender() on it, so the synthesiser and the bar get the raw marker: "תגיד«י|» לי
    שוב". Every other reply goes through respond()/finish(), which do."""
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    upstream.mode = "status:500"
    d, _ = m.say(UTTER["hebrew"])
    assert "\u00ab" not in d["say"], d["say"]


@pytest.mark.parametrize("round_", range(3))
def test_blame_free_line_on_outage(tmp_path, proxy_proc, micmic, upstream, round_):
    """FAILURE. Same outage, spoken in her profile language this time. The line must not
    ask her to rephrase: nothing she says differently will fix an outage."""
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    upstream.mode = "status:500"
    d, _ = m.say(UTTER["hebrew"])
    assert_heard(d, "hebrew")
    assert "לא הבנתי" not in d["say"] and d["did"] != "error", d


@pytest.mark.parametrize("round_", range(3))
def test_proxy_down_mid_session(tmp_path, proxy_proc, micmic, round_):
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    assert m.say(UTTER["english"])[0]["did"] != "error"
    p.stop()
    d, took = m.say(UTTER["english"])
    print(round_, json.dumps({k: d.get(k) for k in ("did", "say", "lang")},
                             ensure_ascii=False), f"{took:.1f}s")
    assert took < 10
    assert_heard(d, "english")


@pytest.mark.parametrize("round_", range(3))
def test_revoked_mid_session_says_sign_in_again(tmp_path, proxy_proc, micmic, round_):
    """FAILURE. The proxy answers 401, savta.jev raises NotConfigured, and the server has
    no branch for it: she hears the generic "did not understand" line, /api/health keeps
    saying configured: true, and every later request repeats it. Expected: the same
    not_configured / sign-in line a fresh install gets, in her language."""
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    assert m.say(UTTER["english"])[0]["did"] != "error"
    r = p.admin("revoke", "--token-stdin", stdin=tok + "\n")
    assert r.returncode == 0
    d, _ = m.say(UTTER["english"])
    print(round_, json.dumps({k: d.get(k) for k in ("did", "say", "lang")},
                             ensure_ascii=False), m.health())
    assert d["did"] == "not_configured", d
    assert_heard(d, "english")
    assert m.health()["configured"] is False


@pytest.mark.parametrize("round_", range(3))
def test_credentials_written_after_start_are_picked_up(tmp_path, proxy_proc, micmic,
                                                       round_):
    """FAILURE. jev.mode() is documented as "resolved fresh each time, so a credentials
    file written after startup is picked up by the next client that is created", but
    server.py builds _jev once at import and router.py builds LLM_CLIENT once at import.
    Signing in after launch does nothing until the app restarts."""
    p = proxy_proc()
    m = micmic(profile=HEBREW_PROFILE)
    d, _ = m.say(UTTER["english"])
    assert d["did"] == "not_configured"
    assert_heard(d, "english")
    p.mint(creds=m.state / "credentials.json")
    d, _ = m.say(UTTER["english"])
    print(round_, d.get("did"), m.health())
    assert d["did"] != "not_configured", "credentials written after start were ignored"


@pytest.mark.parametrize("creds_text", [
    '{"proxy_url": "http://proxy.example.com:8815", "token": "mmp_x"}',
    '{"proxy_url": "http://127.0.0.1.nip.io:8815", "token": "mmp_x"}',
    '{"proxy_url": "http://localhost@evil.example:8815", "token": "mmp_x"}',
    '{"proxy_url": "ftp://127.0.0.1:8815", "token": "mmp_x"}',
    '{"proxy_url": "http://127.0.0.1:8815", "token": ""}',
    '{"proxy_url": "http://127.0.0.1:8815"',
    '["http://127.0.0.1:8815", "mmp_x"]',
    '"just a string"',
    '',
    'UNREADABLE',
])
def test_unusable_credentials_start_unconfigured(tmp_path, micmic, creds_text):
    state = tmp_path / "state"
    state.mkdir()
    c = state / "credentials.json"
    if creds_text == "UNREADABLE":
        c.write_text('{"proxy_url": "http://127.0.0.1:8815", "token": "mmp_x"}')
        c.chmod(0)
    else:
        c.write_text(creds_text)
    try:
        m = micmic(profile=HEBREW_PROFILE)
        assert m.health()["configured"] is False
        d, _ = m.say(UTTER["russian"])
        assert d["did"] == "not_configured"
        assert_heard(d, "russian")
    finally:
        c.chmod(0o600)


def test_https_proxy_url_to_a_dead_host_still_answers(tmp_path, micmic):
    """https to anywhere is allowed by design; a host that does not answer must still
    produce a sentence in her language, and not after a minute of silence."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "credentials.json").write_text(json.dumps(
        {"proxy_url": "https://127.0.0.1:9", "token": "mmp_x"}))
    m = micmic(profile=HEBREW_PROFILE)
    d, took = m.say(UTTER["english"])
    print(json.dumps({k: d.get(k) for k in ("did", "say", "lang")}, ensure_ascii=False),
          f"{took:.1f}s")
    assert took < 10
    assert_heard(d, "english")


@pytest.mark.slow
@pytest.mark.parametrize("round_", range(3))
def test_jev_hang_with_production_timeouts(tmp_path, proxy_proc, micmic, upstream, round_):
    """FAILURE. Production timeouts: client 12 s x 3 attempts, proxy 15 s, no refund on
    timeout. One hung Jev call: she waits ~36 s and loses 3 units of her day."""
    p, m, tok = _setup(tmp_path, proxy_proc, micmic)
    upstream.mode, upstream.hang = "hang", 60
    d, took = m.say(UTTER["english"], timeout=180)
    time.sleep(16)        # the proxy's last wait on the upstream runs out
    used = p.db_used(tok)
    print(round_, json.dumps({k: d.get(k) for k in ("did", "say", "lang")},
                             ensure_ascii=False), f"{took:.1f}s", f"units={used}")
    upstream.mode = "ok"
    assert used <= 1, f"{used} units for one question"
    assert took < 20, f"{took:.0f}s of silence"

"""savta/jev.py and savta/llm.py in each mode, against the local proxy and fake upstream.

Nothing here talks to the real Jev or Gemini: the own-key path is pointed at the fake
upstream by overriding the connection target after construction.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import FAKE_JEV_KEY, GEMINI_ANSWER, JEV_ANSWER
from proxy.store import hash_token

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from savta import jev as jevmod  # noqa: E402
from savta import llm as llmmod  # noqa: E402
from savta.jev import Jev, NotConfigured, QuotaExceeded  # noqa: E402
from savta.llm import LLM  # noqa: E402

QS = {"intent": {"type": "choice", "options": ["music", "call"]}}


class Recorder:
    """Counts every TCP connection made to it. Used as a proxy that must never be called."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            c.close()

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No key anywhere, a private state dir, and .env.local made invisible, so each test
    states exactly which credentials exist."""
    for k in ("TYPESAFE_API_KEY", "GEMINI_API_KEY", "MICMIC_PROXY_URL", "MICMIC_PROXY_TOKEN",
              "MICMIC_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MICMIC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(jevmod, "_env_local", lambda name: None)
    monkeypatch.setattr(llmmod, "_key", lambda: None)
    return tmp_path / "state"


@pytest.fixture
def recorder():
    r = Recorder()
    yield r
    r.close()


def use_proxy(monkeypatch, proxy, token):
    monkeypatch.setenv("MICMIC_PROXY_URL", proxy.url)
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", token)


# ------------------------------------------------------------------ mode selection

def test_nothing_configured_raises_a_clear_error(clean_env):
    assert jevmod.mode() == "none"
    with pytest.raises(NotConfigured) as e:
        Jev()
    assert isinstance(e.value, RuntimeError)          # old `except RuntimeError` still works
    assert "credentials.json" in str(e.value) and "TYPESAFE_API_KEY" in str(e.value)
    assert LLM().available is False and LLM().mode == "none"


def test_own_key_wins_over_proxy_credentials(clean_env, monkeypatch, recorder):
    monkeypatch.setenv("TYPESAFE_API_KEY", "own-key-123")
    monkeypatch.setenv("MICMIC_PROXY_URL", f"http://127.0.0.1:{recorder.port}")
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", "mmp_whatever")
    j = Jev()
    assert j.mode == "own_key" and j.key == "own-key-123"
    assert (j._host, j._path) == ("api.typesafe.ai", "/v1/systemone")
    # Own Jev key but no Gemini key: Gemini is unavailable, NOT routed via the proxy.
    llm = LLM()
    assert llm.available is False and llm.mode == "none"
    assert llm.generate_content([{"parts": [{"text": "hi"}]}]) is None
    time.sleep(0.1)
    assert recorder.hits == 0


def test_env_credentials_then_credentials_file(clean_env, monkeypatch):
    clean_env.mkdir(parents=True, exist_ok=True)
    (clean_env / "credentials.json").write_text(json.dumps(
        {"proxy_url": "https://proxy.example.com/", "token": "mmp_from_file"}))
    assert jevmod.proxy_credentials() == ("https://proxy.example.com", "mmp_from_file")
    assert jevmod.mode() == "proxy"
    monkeypatch.setenv("MICMIC_PROXY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", "mmp_from_env")
    assert jevmod.proxy_credentials() == ("http://127.0.0.1:9", "mmp_from_env")
    j = Jev()
    assert j.mode == "proxy" and j.key is None and j._path == "/v1/jev"
    assert (j._https, j._host, j._port) == (False, "127.0.0.1", 9)


@pytest.mark.parametrize("url,ok", [
    ("https://proxy.example.com", True),
    ("http://127.0.0.1:8810", True),
    ("http://localhost:8810", True),
    ("http://proxy.example.com", False),       # a bearer token in clear text over a network
    ("ftp://proxy.example.com", False),
    ("proxy.example.com", False),
    ("", False),
])
def test_proxy_url_must_be_https_unless_local(clean_env, monkeypatch, url, ok):
    monkeypatch.setenv("MICMIC_PROXY_URL", url)
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", "mmp_x")
    assert (jevmod.proxy_credentials() is not None) is ok


def test_broken_credentials_file_is_not_configured(clean_env):
    clean_env.mkdir(parents=True, exist_ok=True)
    for text in ("{not json", "[]", '{"proxy_url": "https://x.example"}', '"s"'):
        (clean_env / "credentials.json").write_text(text)
        assert jevmod.mode() == "none", text


def test_forced_proxy_mode_ignores_own_keys(clean_env, monkeypatch, proxy):
    monkeypatch.setenv("TYPESAFE_API_KEY", "own-key-123")
    monkeypatch.setenv("GEMINI_API_KEY", "own-gem-456")
    monkeypatch.setattr(llmmod, "_key", lambda: "own-gem-456")
    monkeypatch.setenv("MICMIC_MODE", "proxy")
    use_proxy(monkeypatch, proxy, proxy.mint())
    assert Jev().mode == "proxy" and LLM().mode == "proxy" and LLM().key is None


# ------------------------------------------------------------------ Jev through the proxy

def test_jev_ask_via_proxy_keeps_cost_accounting(clean_env, monkeypatch, proxy):
    use_proxy(monkeypatch, proxy, proxy.mint())
    j = Jev()
    j.warmup()
    # The client keeps a small pool of warm connections (savta/jev.py _take/_give);
    # asked one at a time, every ask reuses one of them and none is thrown away.
    pooled = {id(c) for c, _at in j._idle}
    for _ in range(3):
        ans = j.ask("The user said: play music", QS)
        assert ans == JEV_ANSWER["answers"]
    assert j.calls == 3 and j.input_tokens == 3000
    assert j.cost_usd == pytest.approx(3000 * 0.042 / 1e6)
    assert pooled and {id(c) for c, _at in j._idle} <= pooled   # kept alive throughout
    hit = proxy.upstream.hits[0]
    assert hit["headers"]["Authorization"] == f"Bearer {FAKE_JEV_KEY}"
    assert json.loads(hit["body"])["questions"] == QS


def test_quota_exceeded_is_raised_at_once_not_retried(clean_env, monkeypatch, proxy):
    tok = proxy.mint()
    use_proxy(monkeypatch, proxy, tok)
    j = Jev()
    for _ in range(5):
        j.ask("s", QS)
    t0 = time.time()
    with pytest.raises(QuotaExceeded) as e:
        j.ask("s", QS)
    assert time.time() - t0 < 0.3                  # no backoff sleeps
    assert e.value.limit == 5 and e.value.resets_at == "2026-09-25T00:00:00Z"
    assert not isinstance(e.value, RuntimeError)
    assert len(proxy.upstream.hits) == 5
    assert j.calls == 5


def test_revoked_token_raises_not_configured(clean_env, monkeypatch, proxy):
    tok = proxy.mint()
    use_proxy(monkeypatch, proxy, tok)
    j = Jev()
    j.ask("s", QS)
    proxy.store.revoke(hash_token(tok))
    with pytest.raises(NotConfigured):
        j.ask("s", QS)


def test_busy_upstream_is_retried_then_fails_like_before(clean_env, monkeypatch, proxy):
    use_proxy(monkeypatch, proxy, proxy.mint())
    proxy.upstream.mode = "busy429"
    j = Jev()
    with pytest.raises(RuntimeError, match="jev call failed: HTTP 503"):
        j.ask("s", QS)
    assert len(proxy.upstream.hits) == 3


def test_proxy_restart_between_calls_is_survived(clean_env, monkeypatch, proxy):
    """The kept-alive connection dies when the proxy closes it; the next ask reconnects."""
    use_proxy(monkeypatch, proxy, proxy.mint())
    j = Jev()
    j.ask("s", QS)
    for c, _at in j._idle:                        # every pooled connection goes dead
        c.sock.shutdown(socket.SHUT_RDWR)
    assert j.ask("s", QS) == JEV_ANSWER["answers"]


# ------------------------------------------------------------------ own key, direct

def test_own_key_goes_direct_and_never_touches_the_proxy(clean_env, monkeypatch, upstream,
                                                         recorder):
    monkeypatch.setenv("TYPESAFE_API_KEY", "own-key-123")
    monkeypatch.setenv("MICMIC_PROXY_URL", f"http://127.0.0.1:{recorder.port}")
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", "mmp_should_never_be_sent")
    j = Jev()
    # Aim the direct path at the fake upstream instead of api.typesafe.ai.
    j._https, j._host, j._port = False, "127.0.0.1", upstream.port
    assert j.ask("s", QS) == JEV_ANSWER["answers"]
    assert j.cost_usd == pytest.approx(1000 * 0.042 / 1e6)
    hit = upstream.hits[0]
    assert hit["path"] == "/v1/systemone"
    assert hit["headers"]["Authorization"] == "Bearer own-key-123"
    # Direct mode still treats Jev's own 429 as retryable, as it always did.
    upstream.mode = "busy429"
    with pytest.raises(RuntimeError, match="HTTP 429"):
        j.ask("s", QS)
    assert len(upstream.hits) == 4
    time.sleep(0.1)
    assert recorder.hits == 0


# ------------------------------------------------------------------ Gemini

def test_generate_content_via_proxy_with_image(clean_env, monkeypatch, proxy):
    use_proxy(monkeypatch, proxy, proxy.mint())
    llm = LLM()
    assert llm.available and llm.mode == "proxy" and llm.key is None
    contents = [{"role": "user", "parts": [
        {"text": "what is on the screen"},
        {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}]}]
    d = llm.generate_content(contents)
    assert d == GEMINI_ANSWER and llm.calls == 1 and llm.last_error is None
    sent = json.loads(proxy.upstream.hits[0]["body"])
    assert sent["contents"] == contents
    # The ordinary text jobs go the same way.
    assert llm.text("hello", system="be brief") == "a calm blue screen"
    assert llm._conn is not None                   # kept alive for the next call


def test_gemini_quota_via_proxy_sets_out_of_credit(clean_env, monkeypatch, proxy):
    use_proxy(monkeypatch, proxy, proxy.mint())
    llm = LLM()
    for _ in range(3):
        assert llm.text("hi") == "a calm blue screen"
    assert llm.text("hi") is None
    assert llm.out_of_credit is True
    assert llm.quota_exceeded.limit == 3
    assert llm.quota_exceeded.resets_at == "2026-09-25T00:00:00Z"


def test_gemini_upstream_failure_via_proxy_is_generic(clean_env, monkeypatch, proxy):
    use_proxy(monkeypatch, proxy, proxy.mint())
    proxy.upstream.mode = "error500"
    llm = LLM()
    assert llm.generate_content([{"parts": [{"text": "x"}]}]) is None
    assert llm.last_error == "HTTP 502 upstream_error"
    assert FAKE_JEV_KEY not in llm.last_error and llm.out_of_credit is False


def test_own_gemini_key_is_used_directly_even_without_a_jev_key(clean_env, monkeypatch,
                                                                 recorder):
    monkeypatch.setattr(llmmod, "_key", lambda: "own-gem-456")
    monkeypatch.setenv("MICMIC_PROXY_URL", f"http://127.0.0.1:{recorder.port}")
    monkeypatch.setenv("MICMIC_PROXY_TOKEN", "mmp_x")
    llm = LLM()
    assert llm.mode == "own_key" and llm.key == "own-gem-456" and llm._proxy is None

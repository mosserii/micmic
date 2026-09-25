"""Gemini, used for exactly two things Jev structurally cannot do.

Jev returns typed judgments and cannot emit text. That is fine for routing and for
selecting among real candidates, which is most of what this assistant does. It is not
fine for two jobs:

  1. splitting "message Zohar and then play music" into ordered steps
  2. answering "who was president in the 70s" out loud

Both sit on paths where she is already waiting, so the extra second costs nothing that
matters. Neither is ever on the routing path.
"""
from __future__ import annotations
import http.client, json, os, re, threading, time, urllib.parse
from pathlib import Path

from . import jev as _jev

MODEL = "gemini-3.5-flash-lite"
BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _key() -> str | None:
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    for p in (Path(__file__).resolve().parents[2] / ".env.local",
              Path(__file__).resolve().parents[1] / ".env.local"):
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
    return None


def _route() -> tuple[str | None, tuple[str, str] | None]:
    """(own Gemini key, proxy credentials); at most one is set.

    Follows jev.mode(): with an own Jev key the proxy is never contacted, so Gemini is
    direct with GEMINI_API_KEY or simply unavailable. Without one, an own Gemini key
    still wins over the proxy, because it costs the owner nothing and keeps the text
    off our server."""
    forced = os.environ.get("MICMIC_MODE", "").strip().lower()
    if forced == "proxy":
        return None, _jev.proxy_credentials()
    if forced == "own_key" or _jev.own_key():
        return _key(), None
    k = _key()
    if k:
        return k, None
    return None, _jev.proxy_credentials()


class LLM:
    def __init__(self, model: str = MODEL, timeout: float = 20.0):
        self.key, proxy = _route()
        self.mode = "own_key" if self.key else ("proxy" if proxy else "none")
        self.model = model
        self.timeout = timeout
        self.calls = 0
        self.last_ms = 0.0
        self.busy_ms = 0.0          # wall time inside _post, failures included
        self.last_error: str | None = None
        # Set when the proxy says today's Gemini allowance is used up, so the router can
        # say when it comes back instead of a generic failure. Cleared on success.
        self.quota_exceeded: _jev.QuotaExceeded | None = None
        self._proxy = None
        if proxy:
            url, token = proxy
            https, host, port, base = _jev._endpoint(url)
            self._proxy = (https, host, port, base, token)
        self._conn = None
        self._used_at = 0.0
        self._lock = threading.Lock()

    def refresh(self, max_idle: float = _jev.IDLE_FRESH) -> None:
        """Called when she starts speaking, like Jev.refresh: an idle connection is
        replaced while she talks, so an answer never starts with a handshake."""
        if not self.available:
            return
        if self._proxy is not None:
            https, host, port = self._proxy[0], self._proxy[1], self._proxy[2]
        else:
            https, host, port = True, urllib.parse.urlsplit(BASE).hostname, 443
        try:
            with self._lock:
                if self._conn is not None and time.time() - self._used_at <= max_idle:
                    return
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
                cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
                conn = cls(host, port, timeout=self.timeout)
                conn.connect()
                self._conn, self._used_at = conn, time.time()
        except Exception:  # noqa: BLE001
            self._conn = None

    @property
    def available(self) -> bool:
        return bool(self.key) or self._proxy is not None

    @property
    def out_of_credit(self) -> bool:
        """The key is fine, the account is not. This is not a transient failure and
        retrying it thirty times in a row just wastes the user's time in silence."""
        e = (self.last_error or "").lower()
        return "quota" in e or "billing" in e or "resource_exhausted" in e

    def _post(self, payload: dict, model: str | None = None,
              timeout: float | None = None) -> dict | None:
        t_in = time.time()
        try:
            return self._post_once(payload, model, timeout)
        finally:
            self.busy_ms += (time.time() - t_in) * 1000

    def _post_once(self, payload: dict, model: str | None = None,
                   timeout: float | None = None) -> dict | None:
        model = model or self.model
        timeout = self.timeout if timeout is None else timeout
        if self._proxy is not None:
            https, host, port, base, token = self._proxy
            return self._post_kept(payload, https, host, port,
                                   f"{base}/v1/gemini/{model}:generateContent",
                                   {"Authorization": f"Bearer {token}"}, timeout, token)
        if not self.key:
            self.last_error = "no GEMINI_API_KEY"
            return None
        # Direct, on a kept-alive connection too. It used to open a fresh TLS
        # connection per call, about 300 ms of every answer (measured with tests/perf/bench.py).
        u = urllib.parse.urlsplit(BASE)
        return self._post_kept(payload, True, u.hostname, 443,
                               f"{u.path}/{model}:generateContent",
                               {"X-goog-api-key": self.key}, timeout, None)

    def _post_kept(self, payload: dict, https: bool, host: str, port: int, path: str,
                   auth: dict, timeout: float, token: str | None) -> dict | None:
        """One call on the kept-alive connection: a screen description already waits on
        Gemini, it should not also wait on a handshake. `token` is set through the
        proxy, whose refusals mean something of their own."""
        body = json.dumps(payload).encode()
        headers = {**auth, "Content-Type": "application/json", "Connection": "keep-alive"}
        t0 = time.time()
        status, raw = 0, b""
        for attempt in (0, 1):
            try:
                with self._lock:
                    reused = self._conn is not None
                    if self._conn is None:
                        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
                        self._conn = cls(host, port, timeout=timeout)
                    self._conn.timeout = timeout
                    if self._conn.sock is not None:
                        self._conn.sock.settimeout(timeout)
                    self._conn.request("POST", path, body=body, headers=headers)
                    resp = self._conn.getresponse()
                    raw, status = resp.read(), resp.status
                    self._used_at = time.time()
                break
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    try:
                        if self._conn: self._conn.close()
                    except Exception: pass
                    self._conn = None
                # An idle keep-alive the server already closed: the request never left,
                # so trying once more on a fresh connection is safe.
                stale = isinstance(e, (http.client.RemoteDisconnected, BrokenPipeError,
                                       ConnectionResetError))
                if reused and stale and attempt == 0:
                    continue
                self.last_error = repr(e)[:160]
                return None
        if status == 200:
            try:
                d = json.loads(raw)
            except ValueError:
                self.last_error = "unparseable response"
                return None
            self.last_ms = (time.time() - t0) * 1000
            self.calls += 1
            self.last_error = None
            self.quota_exceeded = None
            return d
        try:
            d = json.loads(raw)
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        if token is None:
            # Google's own error, whose message is what out_of_credit reads.
            try:
                self.last_error = str(d["error"]["message"])[:160]
            except (KeyError, TypeError):
                self.last_error = f"HTTP {status}"
            return None
        err = str(d.get("error", ""))
        if status == 429 and err == "daily_limit":
            self.quota_exceeded = _jev.QuotaExceeded(int(d.get("limit", 0)),
                                                     str(d.get("resets_at") or ""),
                                                     str(d.get("scope") or "day"))
            # "quota" makes out_of_credit true, which is right: it will not come back
            # by retrying, only at resets_at.
            self.last_error = "daily quota reached"
        elif status == 401:
            self.last_error = "proxy rejected this device's token"
            _jev.revoked(token)
        else:
            self.last_error = f"HTTP {status} {err}".strip()
        return None

    def generate_content(self, contents: list[dict], model: str | None = None,
                         timeout: float = 30.0, *, system: str = "",
                         generation_config: dict | None = None) -> dict | None:
        """Raw Gemini generateContent through whichever mode is active. `contents` may
        carry inlineData image parts. Returns Gemini's JSON as is, or None with
        last_error set."""
        payload: dict = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if generation_config:
            payload["generationConfig"] = generation_config
        return self._post(payload, model=model, timeout=timeout)

    def text(self, prompt: str, system: str = "", max_tokens: int = 400,
             temperature: float = 0.2) -> str | None:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        d = self._post(payload)
        if not d:
            return None
        try:
            parts = d["candidates"][0]["content"].get("parts", [])
            out = "".join(p.get("text", "") for p in parts).strip()
            return out or None
        except Exception:  # noqa: BLE001
            self.last_error = "unparseable response"
            return None

    # ---------------------------------------------------------------- jobs

    def split_steps(self, utterance: str, language: str = "hebrew") -> list[str] | None:
        """One sentence -> ordered standalone requests. Returns None if it is not compound."""
        out = self.text(
            prompt=f'Sentence: "{utterance}"',
            system=(
                "You split a spoken request into the separate things the speaker is asking for.\n"
                "Return ONLY a JSON array of strings, nothing else.\n"
                "Each string must be a complete standalone request in the SAME language as the "
                "input, understandable on its own with no pronouns referring to the other steps.\n"
                "If the sentence asks for only one thing, return an array with that one sentence.\n"
                "Never invent a step the speaker did not ask for. Maximum 4 steps.\n"
                'Example input: "send Zohar a message that I am fine and then play me some music"\n'
                'Example output: ["send Zohar a message that I am fine", "play me some music"]'),
            max_tokens=300, temperature=0.0)
        if not out:
            return None
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            return None
        try:
            steps = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None
        steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()]
        return steps[:4] or None

    @staticmethod
    def _address(language: str, gender: str) -> str:
        """Hebrew, Arabic and Russian conjugate for who is being spoken to, and a model
        left to guess picks one and misgenders the user in every sentence."""
        if language == "english" or gender not in ("feminine", "masculine"):
            return ""
        word = "a woman" if gender == "feminine" else "a man"
        return (f" You are speaking to {word}: use {gender} grammatical forms for every "
                f"verb, pronoun and adjective that refers to them.")

    def chat(self, utterance: str, language: str = "hebrew", name: str = "",
             recent: str = "", gender: str = "") -> str | None:
        """Small talk. Not every sentence is an instruction, and answering "how are
        you" with "say it again in other words" is the rudest thing this can do."""
        lang = {"hebrew": "Hebrew", "arabic": "Arabic", "russian": "Russian"}.get(
            language, "English")
        now = time.localtime()
        who = f" You are talking to {name}." if name else ""
        sys_prompt = (
            f"You are MicMic, a warm voice assistant living on someone's Mac.{who}"
            f"{self._address(language, gender)}\n"
            f"Reply in {lang}. It is {time.strftime('%H:%M on %A', now)}.\n"
            "This is small talk, not a task. Answer like a friendly person would: one "
            "or two short sentences, warm, never formal. You may ask a light question "
            "back.\n"
            "Your answer is read aloud by a speech synthesiser, so plain spoken words "
            "only: no markdown, no lists, no emoji, no URLs, no parentheses.\n"
            "Do NOT list your features and do NOT offer a menu of options unless she "
            "actually asks what you can do. Do not apologise, and never tell her you "
            "did not understand.")
        prompt = (f"Just before this, the conversation was: {recent}\n\n" if recent else "")
        prompt += f"She said: {utterance}"
        out = self.text(prompt, sys_prompt, max_tokens=160, temperature=0.7)
        if not out:
            return None
        out = re.sub(r"[*_#`]+", "", out)
        out = re.sub(r"https?://\S+", "", out)
        return re.sub(r"\s+", " ", out).strip()[:300]

    def answer(self, question: str, language: str = "hebrew", context: str = "",
               gender: str = "", asked_before: str = "") -> str | None:
        """A short spoken answer. It will be read aloud, so no lists and no markdown."""
        lang = {"hebrew": "Hebrew", "arabic": "Arabic", "russian": "Russian"}.get(
            language, "English")
        # A model with no clock answers "what time is it" with an apology, which is a
        # terrible thing to hear from something sitting on your desk. Give it the clock.
        now = time.localtime()
        sys_prompt = (
            f"You answer a person's spoken question. Reply in {lang}."
            f"{self._address(language, gender)}\n"
            f"Right now it is {time.strftime('%H:%M', now)} on "
            f"{time.strftime('%A, %d %B %Y', now)}. Use this whenever the question "
            f"touches on the time, the date, the day of the week, or anything "
            f"happening today, tomorrow or yesterday. Never say you do not have a "
            f"clock or a calendar.\n"
            "Rules: at most two short sentences. Plain spoken words only, because your answer "
            "is read aloud by a speech synthesiser. No markdown, no lists, no bullet points, "
            "no URLs, no emoji, no parentheses.\n"
            "Answer the question directly and warmly. If you genuinely do not know, say so in "
            "one short sentence rather than guessing.\n"
            "You may be given background information. It comes from an automatic search and is "
            "often about the wrong subject. Use it ONLY if it clearly answers this question; "
            "otherwise ignore it completely and answer from your own knowledge. Never repeat "
            "background information that does not match what she asked.")
        # Without this, "and what about Italy" right after "what is the capital of
        # France" was answered as a general question about Italy: the model had no way
        # to know which property of Italy was being asked about.
        prompt = (f"Earlier in this same conversation she asked:\n{asked_before}\n"
                  "If the new question is a short follow-up, it refers to the same "
                  "thing she was asking about before.\n\n" if asked_before else "")
        prompt += (f"Background information, which may or may not be relevant:\n{context}\n\n"
                   if context else "")
        prompt += f"Question: {question}"
        out = self.text(prompt, sys_prompt, max_tokens=320, temperature=0.3)
        if not out:
            return None
        out = re.sub(r"[*_#`]+", "", out)
        out = re.sub(r"https?://\S+", "", out)
        return re.sub(r"\s+", " ", out).strip()[:400]

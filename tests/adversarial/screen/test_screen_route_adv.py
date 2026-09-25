#!/usr/bin/env python3
"""Adversarial tests for the router's screen branch, driven by this suite's own
windows through the REAL screen.context() and the REAL router.handle().

    uv sync
    .venv/bin/python3 tests/adversarial/screen/test_screen_route_adv.py

What is stubbed, and why:
  * router.understand / router._resume: the NLU. Each scenario states the intent it
    is testing; no Jev call is made in-process.
  * router._screen_llm: captures the exact (prompt, system, image) that would go to
    the model and returns a canned line. No model is called in-process.
  * router.get_contacts: a single fake name, so the real address book is never read.
  * screen.context: wrapped (not replaced) by a privacy guard. The real function runs;
    the wrapper refuses and aborts (exit 3) unless the context's target pid is one this
    suite owns and that process is frontmost before and after.

The one scenario that talks to a real model is `server_trace`: a real server on port
8812 with a private MICMIC_STATE_DIR, reading this suite's own synthetic window. Its
responses are only looked at after confirming the server read THIS process's window.
Set ADV_SERVER=0 to skip it.

Every check states the correct behaviour; a FAIL is a failure/leak this suite found.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import common as C  # noqa: I001  (first: private MICMIC_STATE_DIR, NSApplication)
from common import check, note, show, make_window, static_label, pump, RealAppInFront

from AppKit import NSMakeRange  # noqa: E402

from savta.actions import screen  # noqa: E402
from savta import router, trace  # noqa: E402
from test_screen_adv import text_view, editable  # noqa: E402

FAKE_KEY = "sk-" + "proj-" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56"   # built, never literal

HERE = Path(__file__).resolve().parent
PY = str(C.ROOT / ".venv/bin/python3")
TODAY = time.strftime("%Y-%m-%d")
PROFILE = {"setup_complete": True, "step": "done", "name": "Tester", "language": "english",
           "speech_lang": "en-US", "gender": "feminine", "last_briefed": TODAY,
           "created": 1, "uses": 5, "city": "", "emergency_contact": "", "pinned": []}


def write_profile(state_dir: str) -> None:
    Path(state_dir, "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")


write_profile(C.STATE_DIR)

# ------------------------------------------------------------------ guard

ALLOWED_PIDS = {C.MY_PID}
_ORIG_CONTEXT = screen.context


def _guarded(max_chars: int = 6000) -> dict:
    fm = screen.frontmost()
    if fm.get("pid") not in ALLOWED_PIDS:
        raise RealAppInFront(f"router asked for the screen while pid {fm.get('pid')} "
                             f"({fm.get('app')!r}) was frontmost")
    ctx = _ORIG_CONTEXT(max_chars=max_chars)
    if (ctx.get("frontmost") or {}).get("pid") not in ALLOWED_PIDS:
        raise RealAppInFront("context() targeted a pid this suite does not own")
    if screen.frontmost().get("pid") not in ALLOWED_PIDS:
        raise RealAppInFront("focus moved to a real app during context()")
    return ctx


screen.context = _guarded
# router._screen_context swallows exceptions and returns None, which would hide an
# abort as an "empty" answer. Re-raise the guard's refusal instead.
_ORIG_SCREEN_CONTEXT = router._screen_context


def _screen_context_reraising(max_chars: int = 6000):
    return _guarded(max_chars)


router._screen_context = _screen_context_reraising

# ------------------------------------------------------------------ stubs

CAPTURED: list[dict] = []
LLM_REPLY = {"text": "STUB_MODEL_REPLY"}


def _fake_screen_llm(prompt, system, image=None, max_tokens=300):
    CAPTURED.append({"prompt": prompt, "system": system, "image": image,
                     "max_tokens": max_tokens})
    t = LLM_REPLY["text"]
    return t(prompt) if callable(t) else t


class _FakeLLM:
    available = True
    last_ms = 0.0
    quota_exceeded = None

    def chat(self, *a, **k):
        return None

    def split_steps(self, *a, **k):
        return None


class _FakeJev:
    calls = 0
    cost_usd = 0.0

    def ask(self, *a, **k):
        raise AssertionError("Jev must not be called in-process")


def fake_u(intent: str, **kw) -> dict:
    u = {k: 0.0 for k in (
        "intent_confidence", "wants_full_length", "names_title", "contact_confidence",
        "contact_named", "has_message_content", "money_involved", "sounds_coached",
        "control_confidence", "wants_recent", "asking_for_notes", "is_complete", "noise",
        "refers_back", "is_compound", "needs_knowledge", "about_weather", "about_clock",
        "setting_emergency_contact", "inside_an_app", "speaker_gender_confidence",
        "rejects_last", "describes_instead", "refers_to_screen", "distress", "emergency")}
    u.update({"raw": {}, "intent": intent, "intent_probs": {}, "media_kind": "not_applicable",
              "contact": "nobody", "control_action": "not_applicable",
              "player_action": "not_applicable", "channel": "not_applicable",
              "file_kind": "any", "when_minutes": "none", "language": "english",
              "speaker_gender": "feminine", "screen_task": "not_applicable"})
    u["intent_confidence"] = 0.95
    u["is_complete"] = 0.95
    u.update(kw)
    return u


CURRENT_U: dict = {}
router.understand = lambda j, utt, contacts, *a, **k: CURRENT_U
router.get_contacts = lambda *a, **k: ["Test Person"]
router.LLM_CLIENT = _FakeLLM()
router._screen_llm = _fake_screen_llm


def _fake_resume(j, utterance, contacts):
    slot = router.AWAITING
    router.AWAITING = None
    return {**slot, "contact": "Test Person"} if slot else None


router._resume = _fake_resume
JEV = _FakeJev()


def ask(utterance: str, u: dict) -> dict:
    global CURRENT_U
    CURRENT_U = u
    CAPTURED.clear()
    if ALLOWED_PIDS == {C.MY_PID}:
        C.assert_front_is_me("before handle()")
    out = router.handle(JEV, utterance, speak=False, client="native")
    return out


def reset_state():
    router.AWAITING = None
    router.new_conversation()
    Path(C.STATE_DIR, "trace.jsonl").write_text("", encoding="utf-8")


def trace_text() -> str:
    p = Path(C.STATE_DIR, "trace.jsonl")
    return p.read_text(encoding="utf-8") if p.exists() else ""


def state_blob(state_dir: str) -> str:
    out = []
    for p in Path(state_dir).iterdir():
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


# ------------------------------------------------------------------ windows

SECRETS = [
    ("Your code is 482913", "482913"),
    ("Paid with card 4111 1111 1111 1111, thank you", "4111 1111 1111 1111"),
    ("Transfer to GB82WEST12345698765432 by Friday", "GB82WEST12345698765432"),
    ("key = " + FAKE_KEY, FAKE_KEY),
]
FIELD_SECRETS = [("קוד אימות", "771246"), ("Card number (linked)", "5500 0000 0000 0004")]
ORDINARY = "Pharmacy order ORDER-ZX81 is ready for pickup on Thursday."


def secrets_window():
    win = make_window("Inbox", w=640, h=440)
    cv = win.contentView()
    y = 400
    cv.addSubview_(static_label((20, y, 600, 24), ORDINARY))
    y -= 36
    for line, _ in SECRETS:
        cv.addSubview_(static_label((20, y, 600, 24), line))
        y -= 36
    otp = editable((200, y, 300, 22), FIELD_SECRETS[0][1], ax_label=FIELD_SECRETS[0][0])
    cv.addSubview_(otp)
    y -= 36
    lab = static_label((20, y, 170, 22), "Card number")
    card = editable((200, y, 300, 22), FIELD_SECRETS[1][1])
    card.setAccessibilityTitleUIElement_(lab)
    cv.addSubview_(lab)
    cv.addSubview_(card)
    show(win)
    return win


def all_secret_tokens():
    return [t for _, t in SECRETS] + [v for _, v in FIELD_SECRETS]


# ------------------------------------------------------------------ scenarios

def t_describe_secrets():
    reset_state()
    secrets_window()
    out = ask("what is on my screen", fake_u("screen", screen_task="describe",
                                              refers_to_screen=0.95))
    check("describe: routed to the model (did=described_screen)",
          out["did"] == "described_screen", out["did"])
    check("describe: exactly one model call", len(CAPTURED) == 1, len(CAPTURED))
    if not CAPTURED:
        return
    prompt = CAPTURED[0]["prompt"]
    check("describe: ordinary screen text reaches the model", "ORDER-ZX81" in prompt)
    for tok in all_secret_tokens():
        check(f"describe: secret {tok[:24]!r} never reaches the model prompt",
              tok not in prompt, "LEAK: in prompt sent to model")
    check("describe: no image without Screen Recording", CAPTURED[0]["image"] is None)


def t_read_aloud_focused_field():
    """The secrets window's first editable field (an OTP field labelled in Hebrew)
    is the window's initial first responder, exactly as a login/verification form
    opens. read_aloud prefers the focused field's value over everything else."""
    reset_state()
    secrets_window()
    out = ask("read me what is on the screen", fake_u("screen", screen_task="read_aloud",
                                                      refers_to_screen=0.95))
    said = out.get("say") or ""
    note(f"read_aloud with a focused OTP field: did={out['did']} said={said!r}")
    check("read_aloud focused: routed (did=read_screen)", out["did"] == "read_screen", out["did"])
    check("read_aloud focused: no model involved", not CAPTURED, len(CAPTURED))
    check("read_aloud focused: the focused OTP field's code is never spoken",
          "771246" not in said, "LEAK: spoken aloud (said) with no model in between")
    check("read_aloud focused: the code is not written to trace.jsonl",
          "771246" not in trace_text(), "LEAK: trace.jsonl 'said' holds the code")


def t_read_aloud_visible():
    """No field, nothing selected: read_aloud falls through to visible text."""
    reset_state()
    win = make_window("Inbox", w=640, h=300)
    cv = win.contentView()
    y = 250
    for line in [ORDINARY] + [ln for ln, _ in SECRETS]:
        cv.addSubview_(static_label((20, y, 600, 24), line))
        y -= 36
    show(win)
    win.makeFirstResponder_(None)
    pump(0.1)
    out = ask("read me what is on the screen", fake_u("screen", screen_task="read_aloud",
                                                      refers_to_screen=0.95))
    said = out.get("say") or ""
    check("read_aloud visible: routed (did=read_screen)", out["did"] == "read_screen", out["did"])
    for _, tok in SECRETS:
        check(f"read_aloud visible: secret {tok[:24]!r} is never spoken",
              tok not in said, "LEAK: spoken aloud (said) with no model in between")
    check("read_aloud visible: window chrome ('zoom the window' AXHelp) is not spoken",
          "zoom the window" not in said, f"said ends ...{said[-70:]!r}")
    check("read_aloud visible: the ' | ' join separator is not spoken", " | " not in said,
          f"said={said[:90]!r}")
    tr = trace_text()
    for _, tok in SECRETS:
        check(f"read_aloud visible trace: secret {tok[:24]!r} is not written to trace.jsonl",
              tok not in tr, "LEAK: trace.jsonl 'said' holds it")
    check("read_aloud visible trace: ordinary screen text is not written to trace.jsonl",
          "ORDER-ZX81" not in tr,
          "trace.jsonl 'said' holds screen content (router.py comment: 'the screen itself "
          "never goes into the trace')")


def t_read_aloud_cap():
    reset_state()
    body = "".join(f"word{i:04d} " for i in range(1000))  # 9000 chars, spaced
    win = make_window("Long doc", w=600, h=340)
    tv = text_view(win, body)
    show(win)
    win.makeFirstResponder_(tv)
    tv.setSelectedRange_(NSMakeRange(0, 0))
    pump(0.2)
    out = ask("read it to me", fake_u("screen", screen_task="read_aloud", refers_to_screen=0.9))
    said = out.get("say") or ""
    note(f"read_aloud cap: spaced 9000-char doc -> said {len(said)} chars "
         f"(READ_ALOUD_MAX={router.READ_ALOUD_MAX})")
    check("read_aloud cap: spoken text <= READ_ALOUD_MAX + '...'",
          len(said) <= router.READ_ALOUD_MAX + 3, len(said))
    check("read_aloud cap: cut on a word boundary", said.endswith("...") and
          said[:-3].split(" ")[-1].startswith("word") and len(said[:-3].split(" ")[-1]) == 8,
          said[-30:])
    C.close_all_windows()

    # One short word, then 2000 characters with no spaces (Japanese, a long URL).
    reset_state()
    body = "Menu " + "日本語のテキスト" * 250
    win = make_window("Spaceless", w=600, h=340)
    tv = text_view(win, body)
    show(win)
    win.makeFirstResponder_(tv)
    pump(0.2)
    out = ask("read it to me", fake_u("screen", screen_task="read_aloud", refers_to_screen=0.9))
    said = out.get("say") or ""
    check("read_aloud cap: spaceless text after one word is not cut down to that word",
          len(said) > 100, f"said={said!r} ({len(said)} chars) of a {len(body)}-char text")


def t_translate_selection():
    reset_state()
    he = "ההזמנה QX-7731 תגיע ביום שלישי בבוקר, נא להיות בבית."
    win = make_window("Hebrew note", w=600, h=340)
    tv = text_view(win, "שורה ראשונה.\n" + he + "\nשורה אחרונה.")
    show(win)
    win.makeFirstResponder_(tv)
    start = ("שורה ראשונה.\n").__len__()
    tv.setSelectedRange_(NSMakeRange(start, len(he)))
    pump(0.2)
    # A model's translation carries the screen's content; echo the selection's
    # untranslatable token back the way a real translation would.
    LLM_REPLY["text"] = lambda prompt: "Order QX-7731 arrives Tuesday morning, please be home."
    try:
        out = ask("translate this", fake_u("screen", screen_task="translate",
                                           refers_to_screen=0.95))
    finally:
        LLM_REPLY["text"] = "STUB_MODEL_REPLY"
    check("translate: did=translated_screen", out["did"] == "translated_screen", out["did"])
    if CAPTURED:
        p = CAPTURED[0]["prompt"]
        check("translate: the selection is what is sent, first",
              p.find("Selected text:\n" + he) > 0, p[:200])
        check("translate: max_tokens 400", CAPTURED[0]["max_tokens"] == 400)
    tr = trace_text()
    check("translate trace: the translated screen content is not written to trace.jsonl",
          "QX-7731" not in tr, "LEAK: trace.jsonl 'said' holds the translation of the screen")


def t_send_this_huge():
    reset_state()
    big = "".join(f"p{i:06d} " for i in range(25000))  # 200k chars
    win = make_window("Huge selection", w=600, h=340)
    tv = text_view(win, big)
    show(win)
    win.makeFirstResponder_(tv)
    tv.setSelectedRange_(NSMakeRange(0, len(big)))
    pump(0.3)
    t0 = time.time()
    out = ask("send this", fake_u("message", refers_to_screen=0.9, contact="nobody",
                                  contact_named=0.0))
    dt = time.time() - t0
    check("send this: asks who (need_who)", out["did"] == "need_who", out["did"])
    body = (router.AWAITING or {}).get("body") or ""
    note(f"send this, 200k selection: body armed={len(body)} chars, handle() {dt * 1000:.0f}ms, "
         f"say={out.get('say')!r}")
    check("send this: 4000-char cap applied to the body", len(body) == 4000, len(body))
    check("send this: a body cut from 200000 to 4000 chars is announced, not silent",
          "trunc" in json.dumps(out.get("detail") or {}) or "4000" in (out.get("say") or ""),
          f"say={out.get('say')!r} detail={out.get('detail')!r}")
    # Second turn: she names the contact. The send gate is closed (never opened here),
    # so the router answers send_disabled, but what does it write down?
    out2 = ask("to Test Person", fake_u("message"))
    check("send this (turn 2): gate closed, nothing sent", out2["did"] == "send_disabled",
          out2["did"])
    tr = trace_text()
    check("send this (turn 2): the selected screen text is not written to trace.jsonl",
          "p000000" not in tr,
          f"LEAK: trace detail.text holds {tr.count('p0')} tokens of the selection")


def t_send_this_otp():
    reset_state()
    doc = "Bank alert. Your code is 482913. Do not share it with anyone."
    win = make_window("Messages-like", w=600, h=340)
    tv = text_view(win, doc)
    show(win)
    win.makeFirstResponder_(tv)
    tv.setSelectedRange_(NSMakeRange(doc.index("Your code"), len("Your code is 482913")))
    pump(0.2)
    out = ask("send this to Test Person", fake_u("message", refers_to_screen=0.9,
                                                 contact="nobody", contact_named=0.0))
    body = (router.AWAITING or {}).get("body") or ""
    check("send OTP: a selected one-time code is not armed as a message body unchecked",
          "482913" not in body,
          f"LEAK/SAFETY: body={body!r}; no money/coached check runs on a screen body "
          f"(router.py message branch), and the resumed path arms it with CANCEL_WINDOW")
    out2 = ask("to Test Person", fake_u("message"))
    check("send OTP (turn 2): the code is not written to trace.jsonl",
          "482913" not in trace_text(), "LEAK: trace detail.text holds the OTP")
    check("send OTP (turn 2): gate closed, nothing sent", out2["did"] == "send_disabled")


def t_front_is_me_stub():
    base = {"permissions": {"accessibility": True}}
    cases = [
        ("bundled app", {"app": "MicMic", "bundle_id": "com.betterfly.micmic"}, None, True),
        ("name only", {"app": "MicMic", "bundle_id": ""}, None, True),
        ("MicMic web UI in Chrome", {"app": "Google Chrome", "bundle_id": "com.google.Chrome"},
         {"url": "http://127.0.0.1:8799/", "title": "MicMic"}, True),
        ("MicMic web UI in Safari", {"app": "Safari", "bundle_id": "com.apple.Safari"},
         {"url": "http://localhost:8799/", "title": "MicMic"}, True),
    ]
    for name, fm, page, want in cases:
        got = router._front_is_me({**base, "frontmost": fm, "page": page})
        check(f"_front_is_me: {name} -> {want}", got == want, f"got {got}")


def _build_micmic_app(dirpath: str) -> Path:
    app = Path(dirpath, "MicMic.app")
    (app / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents/Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>MicMic</string>
<key>CFBundleDisplayName</key><string>MicMic</string>
<key>CFBundleIdentifier</key><string>org.adversarial.micmic-name-probe</string>
<key>CFBundleExecutable</key><string>MicMic</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>""", encoding="utf-8")
    exe = app / "Contents/MacOS/MicMic"
    exe.write_text(f'#!/bin/sh\nexec "{PY}" "{HERE / "micmic_helper.py"}" "$@"\n')
    exe.chmod(0o755)
    return app


def t_front_is_me_real():
    """A real frontmost process whose bundle is named MicMic (launched by this suite,
    showing only synthetic text, terminated by its own pid)."""
    reset_state()
    tmp = tempfile.mkdtemp(prefix="micmic-adv-app-")
    app = _build_micmic_app(tmp)
    pidfile = Path(tmp, "pid")
    make_window("Keeper", w=200, h=100)  # so activate_self() has a window to raise later
    subprocess.run(["open", "-n", str(app), "--args", str(pidfile)], check=False)
    pid = 0
    deadline = time.time() + 10
    while time.time() < deadline:
        pump(0.1)
        if pidfile.exists() and pidfile.read_text().strip():
            pid = int(pidfile.read_text().strip())
            break
    if not pid:
        check("front_is_me real: helper app started", False, "no pid file")
        return
    try:
        fm = {}
        deadline = time.time() + 5
        while time.time() < deadline:
            fm = screen.frontmost()
            if fm.get("pid") == pid:
                break
            time.sleep(0.2)
        if fm.get("pid") != pid:
            raise RealAppInFront(f"helper pid {pid} never became frontmost "
                                 f"(frontmost pid {fm.get('pid')})")
        note(f"front_is_me real: frontmost reported app={fm.get('app')!r} "
             f"bundle_id={fm.get('bundle_id')!r}")
        ALLOWED_PIDS.clear()
        ALLOWED_PIDS.add(pid)
        out = ask("what is on my screen", fake_u("screen", screen_task="describe",
                                                  refers_to_screen=0.95))
        check("front_is_me real: a frontmost app named MicMic is refused (front_is_me)",
              out["did"] == "screen_unavailable" and (out.get("detail") or {}).get("why") ==
              "front_is_me", f"did={out['did']} detail={out.get('detail')}")
        check("front_is_me real: nothing went to the model", not CAPTURED, len(CAPTURED))
    finally:
        ALLOWED_PIDS.clear()
        ALLOWED_PIDS.add(C.MY_PID)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        C.activate_self()


# ------------------------------------------------------------------ real server

SERVER_PORT = 8812


def _post(path: str, payload: dict, box: dict):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{SERVER_PORT}{path}",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        box["res"] = json.loads(urllib.request.urlopen(req, timeout=90).read())
    except Exception as e:  # noqa: BLE001
        box["err"] = repr(e)[:200]


def _post_pumping(path: str, payload: dict) -> dict:
    """POST from a worker thread while the main thread keeps this app's run loop
    alive: the server's AX reads of our window are served on OUR main thread."""
    box: dict = {}
    t = threading.Thread(target=_post, args=(path, payload, box))
    t.start()
    while t.is_alive():
        pump(0.05)
    return box


def t_server_trace():
    if os.environ.get("ADV_SERVER", "1") == "0":
        note("server_trace skipped (ADV_SERVER=0)")
        return
    sdir = tempfile.mkdtemp(prefix="micmic-adv-server-")
    write_profile(sdir)
    env = {k: v for k, v in os.environ.items()
           if k not in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL")}
    env.update(MICMIC_PORT=str(SERVER_PORT), MICMIC_STATE_DIR=sdir)
    err = open(Path(sdir, "server.stderr"), "w")
    # stdout discarded: the server prints the real contact list at startup.
    srv = subprocess.Popen([PY, "-m", "savta.server"], cwd=str(C.ROOT), env=env,
                           stdout=subprocess.DEVNULL, stderr=err)
    poisoned = False
    try:
        deadline = time.time() + 40
        up = False
        while time.time() < deadline and srv.poll() is None:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/", timeout=1)
                up = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        if not up:
            check("server_trace: server came up on 8812", False,
                  f"exit={srv.poll()}")
            return

        win = make_window("Pharmacy", w=640, h=380)
        doc = (f"Dear Ruth, {ORDINARY} Your code is 482913. Paid with card "
               f"4111 1111 1111 1111. Pick it up before Thursday.")
        tv = text_view(win, doc, frame=(20, 120, 600, 220))
        win.contentView().addSubview_(static_label((20, 60, 600, 24),
                                                   "הערה: קוד הזמנה QX-7731 מחכה בדלפק"))
        show(win)
        markers = ["ORDER-ZX81", "482913", "4111 1111 1111 1111", "QX-7731", "Ruth"]
        cases = [("read me what is on my screen", "read"),
                 ("what is on my screen?", "describe"),
                 ("summarize what is on my screen", "summarize")]
        costs = 0.0
        for rep in range(C.REPEAT):
            for utt, tag in cases:
                win.makeFirstResponder_(None)
                C.activate_self()
                C.assert_front_is_me("before POST")
                box = _post_pumping("/api/utterance", {"text": utt, "speak": False,
                                                       "client": "native"})
                front_after = screen.frontmost().get("pid")
                res = box.get("res") or {}
                app_read = ((res.get("detail") or {}).get("app") or "")
                if front_after != C.MY_PID or (app_read and app_read != "python3"):
                    poisoned = True
                    raise RealAppInFront(f"server turn '{tag}': front after={front_after}, "
                                         f"server read app={app_read!r}; discarding")
                costs += float(res.get("cost_usd") or 0)
                note(f"server rep{rep + 1} {tag}: did={res.get('did')} app={app_read!r} "
                     f"chars={(res.get('detail') or {}).get('chars')} err={box.get('err')}")
        blob = state_blob(sdir)
        files = sorted(p.name for p in Path(sdir).iterdir())
        note(f"server state files: {files}; jev cost reported ${costs:.4f}")
        tr = Path(sdir, "trace.jsonl")
        rows = [json.loads(x) for x in tr.read_text().splitlines()] if tr.exists() else []
        for r in rows:
            hits = [m for m in markers if m in json.dumps(r, ensure_ascii=False)]
            note(f"trace row did={r.get('did')}: synthetic markers stored={hits}")
        for m in markers:
            n_rows = sum(1 for r in rows if m in json.dumps(r, ensure_ascii=False))
            check(f"server_trace: synthetic screen content {m!r} is not stored in the state dir",
                  m not in blob, f"LEAK: found in {n_rows}/{len(rows)} trace rows")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
        err.close()
        if poisoned:
            # A real app may have been read by the server: delete its state unread.
            shutil.rmtree(sdir, ignore_errors=True)
        else:
            note(f"server state dir kept for inspection: {sdir}")


R = C.REPEAT
TESTS = [
    ("front_is_me_stub", t_front_is_me_stub, 1),
    ("describe_secrets", t_describe_secrets, R),
    ("read_aloud_focused_field", t_read_aloud_focused_field, R),
    ("read_aloud_visible", t_read_aloud_visible, R),
    ("read_aloud_cap", t_read_aloud_cap, R),
    ("translate_selection", t_translate_selection, R),
    ("send_this_huge", t_send_this_huge, R),
    ("send_this_otp", t_send_this_otp, R),
    ("front_is_me_real", t_front_is_me_real, R),
    ("server_trace", t_server_trace, 1),
]

if __name__ == "__main__":
    import sys
    sys.exit(C.run(TESTS, "adversarial router screen-branch suite"))

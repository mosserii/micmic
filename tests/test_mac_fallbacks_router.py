#!/usr/bin/env python3
"""Lane L3 of the permission fallbacks, at the router: a FaceTime call that is only
READY (FaceTime shows its own Call button, nothing rings until she presses it) is
never announced as a call.

    cd <checkout> && uv run python tests/test_mac_fallbacks_router.py

mac.facetime is the REAL function with sending and calling switched on, behind the
test_micmic wall (loaded the way tests/perf/bench.py loads it): `open facetime://...`
is recorded, never handed to macOS. Jev and Gemini are never reached: only
router.attempt_call and router._call_contact run, which make no model call. Zero
paid calls. Every name is made up.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-macfb-router-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "perf"))
import bench                                   # noqa: E402

tm = bench.load_wall()
router, mac = tm.router, tm.mac
from savta import profile as prof              # noqa: E402
from savta.actions import contacts as book     # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail="") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)))
        print(f"  FAIL  {name}\n          {detail}")
    return bool(ok)


BOOK_ROWS = [("Dana Cohen", "Dana", "+15550000003"), ("Gal Ben Ami", "Gal", "+15550000004"),
             ("Noa Levi", "Noa", "+15550000005")]
BOOK = []
for _n, _f, _p in BOOK_ROWS:
    _r = {"name": _n, "first": _f, "phone": _p,
          "waid": _p.lstrip("+") + "@s.whatsapp.net", "source": "fixture"}
    _r["keys"] = book.keys_for(_n) | book.keys_for(_f)
    BOOK.append(_r)
book.all_contacts = lambda force=False, wait=None: list(BOOK)
book.recent_chats = lambda limit=30: [dict(r) for r in BOOK[:limit]]
book._CACHE.update(at=time.time() * 10, rows=list(BOOK))

# "Calling" in every language MicMic speaks: none of these may be said over a call
# that has not been placed.
CALLING = ("calling", "i am calling", "מתקשרת", "عم بتصل", "звоню")


def says_calling(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in CALLING)


def main():
    real_ft = mac.facetime
    mac.facetime = tm.REAL_FACETIME
    p = prof.load()
    p.update(emergency_contact="Dana Cohen", pinned=["Gal Ben Ami", "Noa Levi"])
    prof.save(p)
    try:
        print("\nthe real facetime(), gates on")
        with tm.gates_on():
            tm.LAUNCHED.clear()
            ok, msg = mac.facetime("Dana Cohen", "+15550000003")
        check("facetime() with a number: (False, facetime_confirm ...), FaceTime handed the URL",
              ok is False and msg.startswith("facetime_confirm")
              and any(str(c[-1]).startswith("facetime://") for c in tm.LAUNCHED),
              (ok, msg, tm.LAUNCHED))

        print("\n_call_contact (an ordinary call)")
        for lang in ("english", "hebrew", "arabic", "russian"):
            with tm.gates_on():
                did, said, det = router._call_contact("Dana Cohen", lang)
            check(f"{lang}: never 'calling' on facetime_confirm",
                  did != "calling" and not says_calling(said)
                  and str(det.get("result", "")).startswith("facetime_confirm"),
                  (did, said, det))

        print("\nattempt_call (the emergency loop)")
        for lang in ("english", "hebrew", "arabic", "russian"):
            with tm.gates_on():
                tm.LAUNCHED.clear()
                out = router.attempt_call(lang)
            opened = [c for c in tm.LAUNCHED if str(c[-1]).startswith("facetime")]
            check(f"{lang}: never 'I am calling' on facetime_confirm",
                  out["did"] != "emergency" and not says_calling(out.get("say", "")),
                  (out["did"], out.get("say")))
            # Router lane L2 (the L3 handoff): the loop stops at the first
            # facetime_confirm, so one FaceTime sheet, never three.
            check(f"{lang}: one FaceTime sheet, and 'FaceTime is ready', not a call",
                  len(opened) == 1 and out["did"] == "call_ready"
                  and out.get("who") == "Dana Cohen"
                  and (out.get("detail") or {}).get("ready") == "Dana Cohen",
                  (len(opened), out))
    finally:
        mac.facetime = real_ft

    real_osa = [c for c in tm.LAUNCHED if c and c[0] == "osascript"]
    check("0 real osascript escapes", not real_osa and not tm.OSA, (real_osa, tm.OSA))
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}\n          {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

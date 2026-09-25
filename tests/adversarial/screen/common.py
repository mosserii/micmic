"""Shared plumbing for the adversarial screen tests.

Import this FIRST, before anything from savta: it points MICMIC_STATE_DIR at a fresh
temp dir so nothing this suite does can touch the real trace/memory/profile, then
reuses tests/screen_harness/harness.py for its NSApplication, its windows, and its
RealAppInFront / activate_self() / safe_read() privacy guard (read, never edited).

PRIVACY RULE (recorded incident, see harness.py's docstring): every content-bearing
read happens only while THIS process is verified frontmost, before AND after the
read. On any mismatch the result is discarded unread and the run aborts with exit 3.
Nothing in this suite ever prints text that did not come from its own windows.
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("MICMIC_ALLOW_SEND") or os.environ.get("MICMIC_ALLOW_CALL"):
    print("REFUSING TO RUN: MICMIC_ALLOW_SEND / MICMIC_ALLOW_CALL is set.")
    sys.exit(2)

STATE_DIR = tempfile.mkdtemp(prefix="micmic-adv-screen-")
os.environ["MICMIC_STATE_DIR"] = STATE_DIR

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from screen_harness import harness as H  # noqa: E402  (creates the NSApplication)
from screen_harness.harness import (  # noqa: E402,F401
    RealAppInFront, activate_self, safe_read, make_window, static_label, pump,
    close_all_windows, _KEEPALIVE,
)
from savta.actions import screen  # noqa: E402

REPEAT = int(os.environ.get("ADV_REPEAT", "3"))
MY_PID = os.getpid()

# name -> list of bools, one per repetition
RESULTS: dict[str, list[bool]] = {}
DETAILS: dict[str, list[str]] = {}
NOTES: list[str] = []


def check(name: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.setdefault(name, []).append(ok)
    if not ok and detail:
        DETAILS.setdefault(name, []).append(str(detail)[:400])
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + ("" if ok or not detail
                                                     else f"\n          {str(detail)[:400]}"))
    return ok


def note(msg: str) -> None:
    NOTES.append(msg)
    print(f"  note  {msg}")


def assert_front_is_me(where: str) -> None:
    fm = screen.frontmost()
    if fm.get("pid") != MY_PID:
        # Do not print fm["window"]: it is a real app's window title.
        raise RealAppInFront(f"{where}: frontmost pid {fm.get('pid')} is not this "
                             f"process ({MY_PID}); app={fm.get('app')!r}")


def guarded_context(max_chars: int = 6000) -> dict:
    """screen.context() with the privacy guard: front before, front after, and the
    context's own frontmost pid (every AX read inside context() targets exactly that
    pid) must be this process."""
    assert_front_is_me("before context()")
    ctx = screen.context(max_chars=max_chars)
    if (ctx.get("frontmost") or {}).get("pid") != MY_PID:
        raise RealAppInFront("context() targeted a pid that is not this process; "
                             "discarding it unread")
    assert_front_is_me("after context()")
    return ctx


def show(win) -> None:
    win.makeKeyAndOrderFront_(None)
    activate_self()
    pump(0.3)
    activate_self()


def pct(samples: list[float], p: float) -> float:
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


def latency_row(label: str, samples: list[float]) -> str:
    ms = [x * 1000 for x in samples]
    return (f"{label:<44} n={len(ms):<3} p50={pct(ms, .5):7.0f}ms  p95={pct(ms, .95):7.0f}ms"
            f"  min={min(ms):6.0f}  max={max(ms):6.0f}  sd={statistics.pstdev(ms):5.0f}")


def summary(title: str) -> int:
    print("\n" + "=" * 78)
    print(title)
    failed = {k: v for k, v in RESULTS.items() if not all(v)}
    for k, v in RESULTS.items():
        tag = "ok  " if all(v) else ("FAIL" if not any(v) else "FLAKY")
        print(f"  {tag} {sum(1 for x in v if not x)}/{len(v)} failed  {k}")
    if NOTES:
        print("\nnotes:")
        for n in NOTES:
            print(f"  - {n}")
    print(f"\n{len(RESULTS) - len(failed)} checks green, {len(failed)} checks failing"
          f"  (repeat={REPEAT}, state_dir={STATE_DIR})")
    return 1 if failed else 0


def run(tests, title: str) -> int:
    print(f"{title}   {time.strftime('%Y-%m-%d %H:%M:%S')}   pid={MY_PID}\n")
    only = set(filter(None, os.environ.get("ADV_ONLY", "").split(",")))
    for name, fn, repeat in tests:
        if only and name not in only:
            continue
        for i in range(repeat):
            print(f"{name}  [rep {i + 1}/{repeat}]")
            try:
                fn()
            except RealAppInFront as e:
                close_all_windows()
                print(f"  ABORT {name}: {e}")
                print("RUN ABORTED: a real application held focus. Result discarded "
                      "unread; nothing further was run.")
                return 3
            except Exception:  # noqa: BLE001
                import traceback
                tb = traceback.format_exc(limit=6)
                check(f"{name} raised", False, tb)
            finally:
                close_all_windows()
    return summary(title)

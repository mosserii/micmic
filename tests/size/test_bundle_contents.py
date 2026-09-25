#!/usr/bin/env python3
"""Checks a built release bundle for the things the installer-size work promised:
no PyObjC test suite, no stray third-party test directories, every module the app
actually needs still importable from the bundle's own interpreter, and the whole
app under its size budget.

    python3 tests/size/test_bundle_contents.py <path to MicMic.app>

Defaults to native/dist/MicMic.app relative to the repo root when no path is given,
since that is where `native/build.sh --release` puts one.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Generous headroom over the ~71 MB this bundle measured at after pruning (down
# from 229 MB unpruned): enough that an ordinary dependency bump does not fail the
# build, but nowhere near the old size, so real bloat still trips it.
SIZE_BUDGET_MB = 110

# Every module the release build needs to actually run: the server, every action
# module, account/update, and the listener with the PyObjC frameworks it drives.
# Mirrors the set the size-lane's runtime import trace was built from.
REQUIRED_IMPORTS = [
    "savta.server", "savta.router", "savta.actions.web", "savta.actions.screen",
    "savta.actions.mac", "savta.account", "savta.update",
    "listener", "panel", "bar",
    "AVFoundation", "Speech", "WebKit", "Quartz", "AppKit", "ApplicationServices",
]

PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def du_bytes(path: Path) -> int:
    out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0]) * 1024


def find_dirs(root: Path, name: str) -> list[Path]:
    return [p for p in root.rglob(name) if p.is_dir()]


def check_no_pyobjc_test_suite(res: Path) -> None:
    hit = res / "lib" / "PyObjCTest"
    check("no PyObjCTest (PyObjC's own test suite)", not hit.exists(),
          f"found at {hit}")


def check_no_stray_test_dirs(res: Path) -> None:
    lib = res / "lib"
    dirs = find_dirs(lib, "tests") + find_dirs(lib, "test")
    check("no third-party test directories under Resources/lib", not dirs,
          ", ".join(str(d) for d in dirs))


def check_no_driver_node(res: Path) -> None:
    # The release build strips Playwright's bundled Node runtime (~116 MB) and
    # fetches one lazily on first web task instead (savta/actions/web.py,
    # ensure_playwright_driver). driver/package -- the small JS driver itself --
    # is expected to still be there.
    node = res / "lib" / "playwright" / "driver" / "node"
    pkg = res / "lib" / "playwright" / "driver" / "package"
    check("Playwright's bundled Node runtime is not shipped", not node.exists(),
          f"found at {node}")
    check("Playwright's JS driver (driver/package) is still shipped", pkg.is_dir(),
          f"missing: {pkg}")


def check_unused_stdlib_pruned(res: Path) -> None:
    py311 = res / "python" / "lib" / "python3.11"
    unused = ["tkinter", "idlelib", "lib2to3", "ensurepip", "pydoc_data",
              "turtledemo", "turtle.py", "distutils", "config-3.11-darwin"]
    present = [n for n in unused if (py311 / n).exists()]
    check("unused stdlib pieces (tkinter, idlelib, ensurepip, ...) are pruned",
          not present, ", ".join(present))
    headers = res / "python" / "include"
    check("build-only C headers are pruned after the launcher is compiled",
          not headers.exists(), f"found at {headers}")


def check_imports(app: Path, res: Path) -> None:
    pybin = res / "python" / "bin" / "python3.11"
    if not pybin.exists():
        check("bundled interpreter is present", False, f"missing: {pybin}")
        return
    script = ("import sys, json\n"
              "mods = " + repr(REQUIRED_IMPORTS) + "\n"
              "failed = {}\n"
              "for m in mods:\n"
              "    try:\n"
              "        __import__(m)\n"
              "    except Exception as e:\n"
              "        failed[m] = repr(e)\n"
              "print(json.dumps(failed))\n")
    env = {"PYTHONPATH": f"{res / 'lib'}:{res / 'app'}"}
    import os
    full_env = dict(os.environ)
    full_env.update(env)
    out = subprocess.run([str(pybin), "-c", script], capture_output=True, text=True,
                          env=full_env)
    if out.returncode != 0:
        check("every required module imports from the bundle", False,
              (out.stderr or out.stdout)[-500:])
        return
    import json as _json
    failed = _json.loads(out.stdout.strip().splitlines()[-1])
    check("every required module imports from the bundle", not failed,
          ", ".join(f"{k}: {v}" for k, v in failed.items()))


def check_size_budget(app: Path) -> None:
    size_mb = du_bytes(app) / (1024 * 1024)
    check(f"app is under the {SIZE_BUDGET_MB} MB size budget "
          f"(measured {size_mb:.1f} MB)", size_mb <= SIZE_BUDGET_MB)


def main() -> None:
    app = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "native" / "dist" / "MicMic.app"
    if not app.exists():
        print(f"no bundle at {app} -- build one first: native/build.sh --release")
        sys.exit(2)
    res = app / "Contents" / "Resources"

    check_no_pyobjc_test_suite(res)
    check_no_stray_test_dirs(res)
    check_no_driver_node(res)
    check_unused_stdlib_pruned(res)
    check_imports(app, res)
    check_size_budget(app)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

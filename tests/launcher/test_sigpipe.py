#!/usr/bin/env python3
"""The release launcher must not let a closed connection kill the app (native/launcher.c).

    uv run python tests/launcher/test_sigpipe.py                      # launcher.c, compiled here
    uv run python tests/launcher/test_sigpipe.py --app native/dist-X/MicMic.app   # a built one
    uv run python tests/launcher/test_sigpipe.py --launcher OLD.c     # some other source
    ... --server-only     # only the local server's hang-ups (the 2026-10-08 path)

1.2.5 quit by itself mid-answer on 2026-10-08. The unified log said only
`exited due to SIGPIPE | sent by MicMic`, with no crash report. launcher.c starts Python
with an isolated config, and isolation also means install_signal_handlers = 0, which is
where a normal python ignores SIGPIPE. So a write to a socket whose far end had closed
(the local server answering a poll that had timed out) killed the whole process.

`python3 listener.py` never had the bug, which is why no other suite caught it. So this
runs the REAL launcher: it builds a throwaway bundle (Contents/MacOS/MicMic, the
interpreter at Resources/python, dependencies at Resources/lib) whose
Resources/app/listener.py is the probe below instead of the app. With --app the
executable is the one from a built, signed MicMic.app, next to that bundle's own
interpreter and lib, so the check is on what ships.

The probe, inside the embedded interpreter:
  1. reports the SIGPIPE disposition Python started with;
  2. writes to a socket whose peer has closed until a write gets EPIPE (deterministic);
  3. serves savta.server's own handler on a free port and lets clients hang up on
     /api/guide/state before the answer, then checks a normal request still answers.

Nothing touches 127.0.0.1:8799, the microphone, the owner's state (MICMIC_STATE_DIR is a
scratch directory) or the network beyond loopback. No Jev or Gemini call.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UV_PYTHONS = Path(os.environ.get("UV_PYTHON_INSTALL_DIR",
                                 Path.home() / ".local/share/uv/python"))

PROBE = r'''
import os, signal, socket, sys, threading, time

def say(*a):
    print("PROBE", *a, flush=True)

d = signal.getsignal(signal.SIGPIPE)
say("sigpipe", "SIG_IGN" if d == signal.SIG_IGN else "SIG_DFL" if d == signal.SIG_DFL else repr(d))

# 2. A write to a peer that has gone.
if not os.environ.get("PROBE_SERVER_ONLY"):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    conn, _ = srv.accept()
    cli.close()
    time.sleep(0.05)
    # The first failure can be ECONNRESET (the peer's RST got here first); the write
    # after that is EPIPE, which is the one that raises SIGPIPE.
    raised = []
    for _ in range(1000):
        try:
            conn.sendall(b"x" * 4096)
        except ConnectionResetError:
            raised.append("ConnectionResetError")
        except OSError as e:
            raised.append(type(e).__name__)
            break
    say("raw", "raised", " ".join(raised) or "nothing")
    conn.close(); srv.close()

# 3. The real local server, clients hanging up before the answer.
try:
    from savta import server as S
except Exception as e:  # noqa: BLE001
    say("server", "skipped", repr(e)[:200])
    sys.exit(0)
httpd = S.Server(("127.0.0.1", 0), S.H)
httpd.daemon_threads = True
port = httpd.server_address[1]
assert port != 8799
threading.Thread(target=httpd.serve_forever, daemon=True).start()
req = b"GET /api/guide/state HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
for _ in range(int(os.environ.get("PROBE_HANGUPS", "300"))):
    c = socket.create_connection(("127.0.0.1", port))
    c.sendall(req)
    c.close()
time.sleep(0.3)
c = socket.create_connection(("127.0.0.1", port), timeout=5)
c.sendall(req)
data = b""
while True:
    b = c.recv(65536)
    if not b:
        break
    data += b
c.close()
httpd.shutdown()
say("server", "answers after hang-ups" if data.startswith(b"HTTP/1.1 200") else "bad answer %r" % data[:80])
'''


def uv_python() -> Path:
    found = sorted(UV_PYTHONS.glob("cpython-3.11.*-macos-*"))
    if not found:
        raise SystemExit("no uv-managed CPython 3.11 (uv python install 3.11)")
    return found[0]


def make_bundle(tmp: Path, app: Path | None, launcher: Path) -> Path:
    """A throwaway .app whose listener.py is the probe. Returns its executable."""
    contents = tmp / "Probe.app" / "Contents"
    macos, res = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True)
    (res / "app").mkdir(parents=True)
    exe = macos / "MicMic"
    if app is not None:
        real = app.resolve() / "Contents"
        shutil.copy2(real / "MacOS" / "MicMic", exe)
        # Out of its bundle the Developer ID signature no longer holds: run as is, the
        # copy was SIGKILLed, and with the Info.plist beside it, it sat at _dyld_start.
        # So it is re-signed ad hoc, without the hardened runtime (whose library
        # validation would then refuse the team-signed libpython). Only the signature
        # changes; check_same_code() below proves the machine code is what ships.
        subprocess.run(["codesign", "--remove-signature", str(exe)], check=True)
        subprocess.run(["codesign", "-f", "-s", "-", str(exe)], check=True,
                       capture_output=True)
        # Symlinks, so the binary's @executable_path rpath reaches the bundle's own
        # libpython, and its stdlib and lib are the ones that ship.
        (res / "python").symlink_to(real / "Resources" / "python")
        (res / "lib").symlink_to(real / "Resources" / "lib")
        (res / "app" / "savta").symlink_to(real / "Resources" / "app" / "savta")
    else:
        py = uv_python()
        # As native/build.sh compiles it, against the same uv-managed interpreter.
        subprocess.run(["clang", "-O2", "-Wall", "-mmacosx-version-min=13.0", "-o", str(exe),
                        str(launcher), f"-I{py}/include/python3.11", f"-L{py}/lib",
                        "-lpython3.11", f"-Wl,-rpath,{py}/lib"], check=True)
        (res / "python").symlink_to(py)
        (res / "lib").symlink_to(sysconfig.get_paths()["purelib"])
        (res / "app" / "savta").symlink_to(ROOT / "savta")
    (res / "app" / "listener.py").write_text(PROBE)
    return exe


def text_section(exe: Path) -> str:
    out = subprocess.run(["otool", "-t", str(exe)], capture_output=True, text=True, check=True)
    return out.stdout.split("\n", 1)[1]          # drop the line naming the file


def run(app: Path | None, launcher: Path, server_only: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="micmic-sigpipe-") as t:
        tmp = Path(t)
        exe = make_bundle(tmp, app, launcher)
        same_code = app is None or (
            text_section(exe) == text_section(app / "Contents" / "MacOS" / "MicMic"))
        state = tmp / "state"
        state.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(MICMIC_STATE_DIR=str(state), MICMIC_PORT="0",
                   MICMIC_SERVER="http://127.0.0.1:9")
        if server_only:
            env["PROBE_SERVER_ONLY"] = "1"
        p = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=120)
    out = p.stdout.splitlines()
    for line in out:
        print("  " + line)
    if p.returncode < 0:
        print(f"  launcher killed by {signal.Signals(-p.returncode).name}")
    elif p.stderr.strip():
        print("  stderr: " + p.stderr.strip()[-600:].replace("\n", "\n  stderr: "))

    def check(name: str, ok: bool) -> bool:
        print(("PASS " if ok else "FAIL ") + name)
        return ok

    lines = set(out)
    ok = [
        check("the probe runs the built app's own launcher code (otool -t identical)"
              if app is not None else f"launcher compiled from {launcher.name}", same_code),
        check("launcher survives (not killed by a signal)", p.returncode == 0),
        check("Python starts with SIGPIPE ignored", "PROBE sigpipe SIG_IGN" in lines),
    ]
    if not server_only:
        ok.append(check("a write to a closed peer raises instead of killing the app",
                        any(l.startswith("PROBE raw raised") and l.endswith("BrokenPipeError") for l in out)))
    ok.append(check("savta.server answers after 300 clients hang up mid-answer",
                    "PROBE server answers after hang-ups" in lines))
    print(f"\n{sum(ok)} passed, {len(ok) - sum(ok)} failed")
    return 0 if all(ok) else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", type=Path, help="a built MicMic.app to take the launcher from")
    ap.add_argument("--launcher", type=Path, default=ROOT / "native" / "launcher.c")
    ap.add_argument("--server-only", action="store_true",
                    help="skip the raw write, so only the server's hang-ups can kill it")
    a = ap.parse_args()
    if a.app is not None and not (a.app / "Contents" / "MacOS" / "MicMic").exists():
        raise SystemExit(f"no launcher in {a.app}")
    raise SystemExit(run(a.app, a.launcher, a.server_only))


if __name__ == "__main__":
    main()

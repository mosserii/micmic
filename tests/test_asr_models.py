#!/usr/bin/env python3
"""savta/asr_models.py: the Better recognition download, against a local file server.

    .venv/bin/python3 tests/test_asr_models.py

No network, no model: the catalog is swapped for three tiny files served from a temp
dir on a free local port, MICMIC_ASR_DIR is a temp dir. Never 8799.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import os
import socketserver
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MICMIC_ASR_DIR"] = tempfile.mkdtemp(prefix="asr-models-test-")
os.environ.pop("MICMIC_ASR_NO_DOWNLOAD", None)
from savta import asr_models as A  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


SERVE = Path(tempfile.mkdtemp(prefix="asr-models-serve-"))
HITS: list[str] = []


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SERVE), **k)

    def log_message(self, *a):
        pass

    def send_head(self):
        HITS.append(f"{self.path} {self.headers.get('Range') or ''}")
        rng = self.headers.get("Range")
        p = SERVE / self.path.lstrip("/")
        if rng and p.exists():
            start = int(rng.split("=")[1].split("-")[0])
            data = p.read_bytes()[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return io.BytesIO(data)
        return super().send_head()


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def put(name: str, data: bytes) -> tuple[str, int, str]:
    (SERVE / name).write_bytes(data)
    return name, len(data), hashlib.sha256(data).hexdigest()


def wheel(name: str, files: dict) -> dict:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    n, size, sha = put(name, buf.getvalue())
    return {"name": n, "url": f"{BASE}/{n}", "size": size, "sha256": sha}


A.RUNTIME = [wheel("rt.whl", {"sherpa_onnx/__init__.py": "X = 1\n",
                              "sherpa_onnx/lib/fake.dylib": "bin"})]
A.HF = BASE + "/{name}"
A.MODELS = {"en": {"engine": "tiny", "kind": "nemo_transducer", "repo": "r", "rev": "v",
                   "license": "test",
                   "files": [put("encoder.int8.onnx", os.urandom(300_000)),
                             put("tokens.txt", b"a 0\nb 1\n")]}}


def wait(lang="en-US", timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        s = A.status(lang)
        if s["state"] != "downloading":
            return s
        time.sleep(0.05)
    return A.status(lang)


def test_states():
    check("a language without a model is Apple's", A.status("he-IL")["state"] == "apple")
    s = A.status("en-US")
    check("English starts with nothing downloaded", s["state"] == "none" and s["done"] == 0, s)
    check("the size is known before the download", s["total"] == A.total_bytes("en"), s)


def test_download_verifies_and_installs():
    A.install("en-US")
    s = wait()
    check("the download finishes ready", s["state"] == "ready", s)
    check("done equals total", s.get("done") == s.get("total"), s)
    check("the runtime is unpacked where the listener looks",
          (Path(s["runtime"]) / "sherpa_onnx" / "__init__.py").exists(), s)
    check("the model files are in place",
          all((Path(s["dir"]) / f[0]).exists() for f in A.MODELS["en"]["files"]), s)
    HITS.clear()
    A.install("en-US")
    check("installing again downloads nothing", wait()["state"] == "ready" and not HITS, HITS)


def test_resume_uses_range():
    d = A.model_dir("en")
    enc = d / "encoder.int8.onnx"
    data = enc.read_bytes()
    (d / ".verified").write_text("")
    (d / ".verified").rename(d / ".unverified")          # not ready any more
    enc.rename(d / "encoder.int8.onnx.part")
    with open(d / "encoder.int8.onnx.part", "r+b") as f:
        f.truncate(100_000)                                # a download cut short
    HITS.clear()
    A.install("en-US")
    s = wait()
    check("a cut download resumes with a Range request",
          any("encoder.int8.onnx bytes=100000-" in h for h in HITS), HITS)
    check("and ends ready with the same bytes", s["state"] == "ready"
          and (d / "encoder.int8.onnx").read_bytes() == data, s)


def test_checksum_mismatch_fails_and_is_not_loaded():
    d = A.model_dir("en")
    (d / ".verified").rename(d / ".unverified2")
    (d / "tokens.txt").rename(d / "tokens.old")
    (SERVE / "tokens.txt").write_bytes(b"a 0\nb 2\n")      # upstream changed the file, same size
    A.install("en-US")
    s = wait()
    check("a changed file upstream fails the install", s["state"] == "failed"
          and "checksum" in s.get("error", ""), s)
    check("and it is never marked ready", not A.is_ready("en"))
    check("the listener sees no dir to load", "dir" not in s, s)


def test_tripwire_refuses_a_real_download_in_a_test():
    """Under a test state dir (MICMIC_STATE_DIR), a non-local URL is refused before any
    request is made, and the tripwire file the test harnesses check is left behind."""
    d = A.model_dir("en")
    (d / "tokens.txt").write_bytes(b"x")                   # not the pinned file: must fetch
    real_hf = A.HF
    A.HF = "https://huggingface.co/{repo}/resolve/{rev}/{name}"
    os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="asr-models-state-")
    try:
        A.install("en-US")
        s = wait()
    finally:
        os.environ.pop("MICMIC_STATE_DIR", None)
        A.HF = real_hf
    check("the install fails instead of downloading", s["state"] == "failed"
          and "test" in s.get("error", ""), s)
    check("and the tripwire is there for the harness", (A.base() / A.TEST_TRIPWIRE).exists())


def main():
    for t in (test_states, test_download_verifies_and_installs, test_resume_uses_range,
              test_checksum_mismatch_fails_and_is_not_loaded,
              test_tripwire_refuses_a_real_download_in_a_test):
        t()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    srv.shutdown()
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

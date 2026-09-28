"""Better recognition: a local speech model per language, downloaded in the app.

Apple's recogniser stays the one that runs while she speaks (the bar's live words); a
local model, when her language has one and it is installed, reads the finished turn and
its text is what is sent. Measured on the owner's own voice (20 English clips x 3,
2026-09-28, scratchpad asr-eval): Apple WER 13.3%, names 69%; Parakeet unified-en
(the model Handy runs, in Handy's runtime) WER 6.2%, names 85%, 0.13 s p50 / 0.23 s p95
after she stops.
Hebrew stays on Apple: Whisper read it better (6.9% vs 17.2%) but took 1.4 s.

Nothing here runs a model. The server downloads and checks the files; the listener
(native/local_asr.py) loads them. What they share is the "asr" field of /api/config:
    {"lang", "engine", "state", "done", "total", "dir", "runtime", "kind", "error"}
Optional and additive: a listener that does not know it keeps working on Apple alone.

Audio never leaves the Mac. What is downloaded is the model and its runtime: files
pinned by revision and SHA-256, so a changed file upstream is refused, not loaded.
The runtime (transcribe.cpp, MIT) is downloaded too, not bundled: 1.8 MB the DMG does
not need until she asks for a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HF = "https://huggingface.co/{repo}/resolve/{rev}/{name}"

# The runtime: transcribe.cpp (MIT), Handy's own engine, ggml + Metal. A ctypes binding
# and one native library, so it loads in the bundled CPython 3.11 with nothing compiled
# for Python. Measured on the owner's voice it matched sherpa-onnx's accuracy exactly
# and was faster (0.13 vs 0.17 s p50) in less memory (955 MB vs 1.17 GB).
RUNTIME_VERSION = "transcribe-cpp-0.2.4"
RUNTIME = [
    {"name": "transcribe_cpp-0.2.4-py3-none-any.whl",
     "url": "https://files.pythonhosted.org/packages/2c/0a/d55134fb1acb43d3cc2d2dd46e23aace14e7"
            "396d364744373263fdc05db9/transcribe_cpp-0.2.4-py3-none-any.whl",
     "size": 34910,
     "sha256": "e4bde0002fea09dc2b573f9b18c9d5d1163630b3096392b3dc82eb5dce25be9d"},
    {"name": "transcribe_cpp_native-0.2.4-py3-none-macosx_11_0_arm64.whl",
     "url": "https://files.pythonhosted.org/packages/50/d1/f9cb064a9d199a9fa09db1dc0f86e7845e30f"
            "444201738f4f61ff18dcb38/transcribe_cpp_native-0.2.4-py3-none-macosx_11_0_arm64.whl",
     "size": 1800676,
     "sha256": "aab340c5d815d83f3737cb793462f1726cf7e6edaeb741b15f6eaf52db0fb909"},
]

# One engine per language; a language missing here is Apple's alone.
# License of each model: in THIRD_PARTY_NOTICES.md.
MODELS = {
    "en": {
        "engine": "parakeet-unified-en-0.6b-q8",
        "kind": "transcribe_cpp",
        # The GGUF Handy ships (its catalog's only public build of this model).
        "repo": "handy-computer/parakeet-unified-en-0.6b-gguf",
        "rev": "d5249700b2382bf5c5024c2421d101b8db54a629",
        "license": "NVIDIA Open Model License (nvidia/parakeet-unified-en-0.6b)",
        "files": [
            ("parakeet-unified-en-0.6b-Q8_0.gguf", 731357568,
             "4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795"),
        ],
    },
}

SPEECH_TO_CODE = {"en-US": "en", "he-IL": "he", "ar-SA": "ar", "ru-RU": "ru"}
CHUNK = 1 << 20


def base() -> Path:
    """Models are hundreds of MB: always Application Support (never the checkout,
    which is the dev state dir), shared by a dev run and the installed app."""
    d = Path(os.environ.get("MICMIC_ASR_DIR")
             or Path.home() / "Library/Application Support/MicMic/asr")
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_dir() -> Path:
    return base() / "runtime" / RUNTIME_VERSION


def model_dir(lang: str) -> Path:
    return base() / "models" / MODELS[lang]["engine"]


def _ready_marker(d: Path) -> Path:
    return d / ".verified"


def is_ready(lang: str) -> bool:
    return (lang in MODELS and _ready_marker(model_dir(lang)).exists()
            and _ready_marker(runtime_dir()).exists())


def total_bytes(lang: str) -> int:
    return sum(f[1] for f in MODELS[lang]["files"]) + sum(r["size"] for r in RUNTIME)


class _Job:
    def __init__(self, lang: str):
        self.lang, self.done, self.error = lang, 0, ""
        self.total = total_bytes(lang)
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None


_LOCK = threading.Lock()
_JOB: _Job | None = None
_FAILED: dict[str, str] = {}


def status(speech_lang: str) -> dict:
    """The "asr" field of /api/config for the language she listens in."""
    lang = SPEECH_TO_CODE.get(speech_lang, "")
    if lang not in MODELS:
        return {"lang": lang, "engine": "apple", "state": "apple"}
    m = MODELS[lang]
    out = {"lang": lang, "engine": m["engine"], "kind": m["kind"], "total": total_bytes(lang)}
    if is_ready(lang):
        return {**out, "state": "ready", "done": out["total"],
                "dir": str(model_dir(lang)), "runtime": str(runtime_dir())}
    job = _JOB
    if job is not None and job.lang == lang and job.thread and job.thread.is_alive():
        return {**out, "state": "downloading", "done": job.done}
    if lang in _FAILED:
        return {**out, "state": "failed", "done": 0, "error": _FAILED[lang]}
    return {**out, "state": "none", "done": 0}


def install(speech_lang: str) -> dict:
    """Start (or keep) the download for her language. Idempotent; one at a time."""
    global _JOB
    lang = SPEECH_TO_CODE.get(speech_lang, "")
    if lang not in MODELS or is_ready(lang):
        return status(speech_lang)
    with _LOCK:
        if _JOB is not None and _JOB.thread and _JOB.thread.is_alive():
            if _JOB.lang == lang:
                return status(speech_lang)
            _JOB.cancel.set()                    # a new language replaces the old job
            _JOB.thread.join(timeout=5)
        _FAILED.pop(lang, None)
        _JOB = job = _Job(lang)
        job.thread = threading.Thread(target=_run, args=(job,), daemon=True,
                                      name="micmic-asr-download")
        job.thread.start()
    return status(speech_lang)


def cancel() -> None:
    job = _JOB
    if job is not None:
        job.cancel.set()


def _run(job: _Job) -> None:
    try:
        _install_runtime(job)
        _install_model(job)
    except _Cancelled:
        pass
    except Exception as e:  # noqa: BLE001
        _FAILED[job.lang] = f"{type(e).__name__}: {e}"[:200]


class _Cancelled(Exception):
    pass


def _fetch(url: str, dest: Path, size: int, sha: str, job: _Job) -> None:
    """Download to dest.part (resumed with Range), check size and SHA-256, then move
    into place. A file already in place with the right hash is counted and kept."""
    if dest.exists() and dest.stat().st_size == size and _sha(dest) == sha:
        job.done += size
        return
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        _truncate(part); have = 0
    job.done += have
    if have < size:
        _get(url, part, have, job)
    if part.stat().st_size != size:
        raise IOError(f"{dest.name}: got {part.stat().st_size} bytes, expected {size}")
    if _sha(part) != sha:
        _truncate(part)                           # the next try starts from nothing
        raise IOError(f"{dest.name}: checksum mismatch")
    part.replace(dest)


def _truncate(p: Path) -> None:
    """Empty a partial download. Nothing in savta/ deletes files (tests/test_micmic.py
    rule 9), and a zero-byte .part is as good as none: the next fetch starts over."""
    with open(p, "wb"):
        pass


TEST_TRIPWIRE = "REAL_DOWNLOAD_IN_TEST"
SEAM_CALLED = "DOWNLOAD_REQUESTED"


def _get(url: str, part: Path, have: int, job: _Job) -> None:
    if os.environ.get("MICMIC_ASR_NO_DOWNLOAD"):
        # The test seam: the job stays "downloading" at 0 and never touches the network.
        # SEAM_CALLED lets a test see that a download was asked for, and of what.
        with open(base() / SEAM_CALLED, "a") as f:
            f.write(url + "\n")
        job.cancel.wait()
        raise _Cancelled()
    if os.environ.get("MICMIC_STATE_DIR") and urllib.parse.urlparse(url).hostname not in (
            "127.0.0.1", "localhost"):
        # MICMIC_STATE_DIR is set by tests only (savta/paths.py). A test that forgot the
        # seam above would pull 733 MB: refuse, and leave a tripwire the test checks.
        (base() / TEST_TRIPWIRE).write_text(url)
        raise IOError("a test tried to download a model for real")
    req = urllib.request.Request(url, headers={"User-Agent": "MicMic"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=30) as r:
        if have and r.status != 206:              # the server ignored the range
            job.done -= have
            _truncate(part)
        with open(part, "ab") as f:
            while True:
                if job.cancel.is_set():
                    raise _Cancelled()
                b = r.read(CHUNK)
                if not b:
                    break
                f.write(b)
                job.done += len(b)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _install_runtime(job: _Job) -> None:
    d = runtime_dir()
    if _ready_marker(d).exists():
        job.done += sum(r["size"] for r in RUNTIME)
        return
    dl = base() / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    for r in RUNTIME:
        _fetch(r["url"], dl / r["name"], r["size"], r["sha256"], job)
    # Extracting again over a half-done folder rewrites every file; the marker, written
    # last, is what says the runtime is whole.
    d.mkdir(parents=True, exist_ok=True)
    for r in RUNTIME:
        with zipfile.ZipFile(dl / r["name"]) as z:
            z.extractall(d)
    for so in d.rglob("*"):
        if so.suffix in (".so", ".dylib"):
            so.chmod(0o755)
    _ready_marker(d).write_text(json.dumps({"at": time.time()}))


def _install_model(job: _Job) -> None:
    m = MODELS[job.lang]
    d = model_dir(job.lang)
    d.mkdir(parents=True, exist_ok=True)
    for name, size, sha in m["files"]:
        _fetch(HF.format(repo=m["repo"], rev=m["rev"], name=name), d / name, size, sha, job)
    _ready_marker(d).write_text(json.dumps({"at": time.time(), "rev": m["rev"]}))

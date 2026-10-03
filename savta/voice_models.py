"""The natural voice: its model and runtime, downloaded in the app.

Settings > Voice > Natural voice downloads these once. Until they are in place, and on
any Mac that cannot run them, MicMic speaks with macOS `say` exactly as before.

Same rules as Better recognition (savta/asr_models.py, whose fetch code this reuses):
every file pinned by URL or revision and SHA-256, resumable with Range, checked before
it is used, and tests never download (MICMIC_ASR_NO_DOWNLOAD holds a job at 0 bytes;
any MICMIC_STATE_DIR process that forgot it trips a wire instead of pulling 380 MB).

English only for now: Kokoro-82M (Apache-2.0), voice af_heart, run on the GPU through
MLX (MIT). MLX needs macOS 14 on Apple silicon; anything else stays on `say`.
Measured on the owner's M1 (2026-10-01, ~/jev/micmic-work/voice): naturalness
(UTMOS, 11 English lines) 4.51 against 4.18 for today's `say`; first audio of a short
first clause ~0.2-0.3 s against 0.17 s for `say`.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
import zipfile
from pathlib import Path

from . import asr_models as _dl

# The runtime: wheels unpacked into one folder that savta/voice.py puts on sys.path.
# Nothing here is compiled for MicMic; these are the PyPI files, byte for byte.
# espeak-ng (GPL-3.0) reads letters into sounds. It is a separate library downloaded
# here on her request, never shipped in the DMG, called through ctypes.
RUNTIME_VERSION = "kokoro-mlx-0.32.3"
_PY = "https://files.pythonhosted.org/packages/"
RUNTIME = [
    {"name": "numpy-2.3.5-cp311-cp311-macosx_14_0_arm64.whl",
     "url": _PY + "4d/1a/e85f0eea4cf03d6a0228f5c0256b53f2df4bc794706e7df019fc622e47f1/"
                  "numpy-2.3.5-cp311-cp311-macosx_14_0_arm64.whl",
     "size": 5356872,
     "sha256": "ffe22d2b05504f786c867c8395de703937f934272eb67586817b46188b4ded6d"},
    {"name": "mlx-0.32.3-cp311-cp311-macosx_14_0_arm64.whl",
     "url": _PY + "a6/ed/7ced6107f0eb4959659e2a3bed2e402b70eb117d3c8941d4c9b21e9c3ca8/"
                  "mlx-0.32.3-cp311-cp311-macosx_14_0_arm64.whl",
     "size": 655739,
     "sha256": "8439baa207db769df1df82a722b0ee0c2362f75c23c0740b7472c3d279253bae"},
    {"name": "mlx_metal-0.32.3-py3-none-macosx_14_0_arm64.whl",
     "url": _PY + "f8/ed/8510985bcb24a6eae359a5441ce4cedcee13b84258a010cd0e515e9fb0bc/"
                  "mlx_metal-0.32.3-py3-none-macosx_14_0_arm64.whl",
     "size": 42205858,
     "sha256": "fdba88ed9131448cf163be2c7ad6374746d8818205f28657aa91b6c69050f691"},
    {"name": "mlx_audio-0.5.7-py3-none-any.whl",
     "url": _PY + "69/6b/352b0fd31af2a2ef6bb3f70d573fa7dd2a4c750e3245505a72f10d2f2d41/"
                  "mlx_audio-0.5.7-py3-none-any.whl",
     "size": 2118941,
     "sha256": "2f2b3c666241ff4a45df7e92057d176b3c1079541a5e9062a0d1e8088db5c123"},
    {"name": "espeakng_loader-0.2.4-py3-none-macosx_11_0_arm64.whl",
     "url": _PY + "a8/26/258c0cd43b9bc1043301c5f61767d6a6c3b679df82790c9cb43a3277b865/"
                  "espeakng_loader-0.2.4-py3-none-macosx_11_0_arm64.whl",
     "size": 9892565,
     "sha256": "d27cdca31112226e7299d8562e889d3e38a1e48055c9ee381b45d669072ee59f"},
]

# One voice per language; a language missing here is spoken by `say`.
# Licenses: THIRD_PARTY_NOTICES.md.
MODELS = {
    "english": {
        "engine": "kokoro-82m-v1.0-af_heart",
        "repo": "mlx-community/Kokoro-82M-bf16",
        "rev": "a71e4d38b236d968966a2002c4c895dbd12b1c3c",
        "voice": "af_heart",
        "license": "Apache-2.0 (hexgrad/Kokoro-82M)",
        "files": [
            ("config.json", 2351,
             "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f"),
            ("kokoro-v1_0.safetensors", 327115152,
             "4e9ecdf03b8b6cf906070390237feda473dc13327cb8d56a43deaa374c02acd8"),
            ("voices/af_heart.safetensors", 522320,
             "2c1c733b0e6576c810e268d3e440c21dea4e0f0131a3ba4cfc98d7fe6136d094"),
        ],
    },
}


def base() -> Path:
    """Application Support, shared by a dev run and the installed app. A test process
    (MICMIC_STATE_DIR) never looks there: it gets a folder of its own."""
    if os.environ.get("MICMIC_VOICE_DIR"):
        d = Path(os.environ["MICMIC_VOICE_DIR"])
    elif os.environ.get("MICMIC_STATE_DIR"):
        d = Path(os.environ["MICMIC_STATE_DIR"]) / "voice-models"
    else:
        d = Path.home() / "Library/Application Support/MicMic/voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_dir() -> Path:
    return base() / "runtime" / RUNTIME_VERSION


def model_dir(lang: str) -> Path:
    return base() / "models" / MODELS[lang]["engine"]


def _ready_marker(d: Path) -> Path:
    return d / ".verified"


def supported() -> bool:
    """MLX runs on Apple silicon with macOS 14 or later. MICMIC_VOICE_SUPPORTED lets a
    test pretend either way."""
    forced = os.environ.get("MICMIC_VOICE_SUPPORTED")
    if forced is not None:
        return forced == "1"
    if platform.machine() != "arm64":
        return False
    try:
        return int(platform.mac_ver()[0].split(".")[0]) >= 14
    except (ValueError, IndexError):
        return False


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
# Called with the language once a download has finished and checked out (the server
# uses it to load the voice straight away, so her next answer already has it).
ON_READY: list = []


def status(lang: str = "english") -> dict:
    """The "voice" field of /api/config and GET /api/voice."""
    if lang not in MODELS:
        return {"lang": lang, "engine": "say", "state": "say"}
    m = MODELS[lang]
    out = {"lang": lang, "engine": m["engine"], "total": total_bytes(lang)}
    if not supported():
        return {**out, "state": "unsupported", "done": 0}
    if is_ready(lang):
        return {**out, "state": "ready", "done": out["total"]}
    job = _JOB
    if job is not None and job.lang == lang and job.thread and job.thread.is_alive():
        return {**out, "state": "downloading", "done": job.done}
    if lang in _FAILED:
        return {**out, "state": "failed", "done": 0, "error": _FAILED[lang]}
    return {**out, "state": "none", "done": 0}


def install(lang: str = "english") -> dict:
    """Start (or keep) the download. Idempotent; one at a time."""
    global _JOB
    if lang not in MODELS or not supported() or is_ready(lang):
        return status(lang)
    with _LOCK:
        if _JOB is not None and _JOB.thread and _JOB.thread.is_alive():
            return status(lang)
        _FAILED.pop(lang, None)
        _JOB = job = _Job(lang)
        job.thread = threading.Thread(target=_run, args=(job,), daemon=True,
                                      name="micmic-voice-download")
        job.thread.start()
    return status(lang)


def cancel() -> None:
    job = _JOB
    if job is not None:
        job.cancel.set()


def _run(job: _Job) -> None:
    try:
        _install_runtime(job)
        _install_model(job)
    except _dl._Cancelled:
        return
    except Exception as e:  # noqa: BLE001
        _FAILED[job.lang] = f"{type(e).__name__}: {e}"[:200]
        return
    for fn in list(ON_READY):
        try:
            fn(job.lang)
        except Exception:  # noqa: BLE001
            pass


def _install_runtime(job: _Job) -> None:
    d = runtime_dir()
    if _ready_marker(d).exists():
        job.done += sum(r["size"] for r in RUNTIME)
        return
    dl = base() / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    for r in RUNTIME:
        _dl._fetch(r["url"], dl / r["name"], r["size"], r["sha256"], job, seam=base())
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
    for name, size, sha in m["files"]:
        dest = d / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        _dl._fetch(_dl.HF.format(repo=m["repo"], rev=m["rev"], name=name), dest, size, sha,
                   job, seam=base())
    _ready_marker(d).write_text(json.dumps({"at": time.time(), "rev": m["rev"]}))

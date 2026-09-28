"""The local model that reads a finished turn (savta/asr_models.py downloads it).

Apple's recogniser still runs while she speaks: its words are what the bar shows. When
her language has a local model and it is installed, the turn's audio (kept in memory
only, never on disk) is read by it once she stops, and that text is what is sent.

Loaded on a background thread, never on a speech callback: a load takes about a second
(the first ever on a Mac, ~36 s, while Metal compiles its shaders once). Until it is loaded, and whenever it fails, the turn
goes out as Apple heard it. Nothing here touches the network.

Freed after IDLE_UNLOAD seconds without a turn (about 1 GB), like Handy's default.
The next turn wakes it the moment she starts speaking, so the reload runs while she
talks; a turn that ends before it is back goes out as Apple heard it.
"""
from __future__ import annotations

import atexit
import math
import sys
from array import array
import threading
import time
from typing import Optional

IDLE_UNLOAD = 300.0     # the owner's choice (2026-09-28): free it after 5 minutes, as Handy does
_REAP_EVERY = 20.0


_OPEN: "list[LocalASR]" = []


@atexit.register
def _close_all() -> None:
    for m in list(_OPEN):
        try:
            m.close()
        except Exception:  # noqa: BLE001
            pass


class LocalASR:
    def __init__(self, info: dict, log=print) -> None:
        self.info = dict(info)
        self.key = (info.get("engine"), info.get("dir"), info.get("runtime"))
        self.log = log
        self.model = None                     # transcribe_cpp.Model; rec is its session
        self.rec = None
        self.error = ""
        self.lock = threading.Lock()          # one decode at a time
        self.loaded = threading.Event()
        self.loading = False
        self.last_used = time.time()
        self._reaper = None
        _OPEN.append(self)

    def load_async(self) -> None:
        self.loading = True
        self.loaded.clear()
        threading.Thread(target=self._load, daemon=True, name="micmic-local-asr").start()
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, daemon=True,
                                            name="micmic-local-asr-idle")
            self._reaper.start()

    def wake(self) -> None:
        """A turn is opening: note it, and bring the model back if idle freed it."""
        self.last_used = time.time()
        if self.rec is None and not self.loading and not self.error:
            self.log("local recogniser: reloading for this turn")
            self.load_async()

    @property
    def usable(self) -> bool:
        """Loaded, or on its way back: worth keeping the turn's samples for."""
        return not self.error and (self.rec is not None or self.loading)

    def free_if_idle(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if self.rec is None or self.loading or now - self.last_used < IDLE_UNLOAD:
            return False
        if not self.lock.acquire(blocking=False):
            return False                      # a turn is being read right now
        try:
            self._release()
        finally:
            self.lock.release()
        self.log(f"local recogniser freed after {int(IDLE_UNLOAD // 60)} idle minutes")
        return True

    def _release(self) -> None:
        rec, model = self.rec, self.model
        self.rec = self.model = None
        self.loaded.clear()
        for h in (rec, model):
            try:
                if h is not None and hasattr(h, "close"):
                    h.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """Before the process ends, or when she changes language. ggml's Metal backend
        asserts at exit (a crash report on her Mac) if a model is still open then."""
        with self.lock:
            self._release()

    def _reap(self) -> None:
        while True:
            time.sleep(_REAP_EVERY)
            try:
                self.free_if_idle()
            except Exception:  # noqa: BLE001
                pass

    def _load(self) -> None:
        t = time.time()
        try:
            rt = str(self.info["runtime"])
            if rt not in sys.path:
                sys.path.insert(0, rt)
            if self.info.get("kind") != "transcribe_cpp":
                raise ValueError(f"unknown model kind {self.info.get('kind')!r}")
            import glob
            import transcribe_cpp
            ggufs = sorted(glob.glob(f"{self.info['dir']}/*.gguf"))
            if not ggufs:
                raise FileNotFoundError(f"no .gguf in {self.info['dir']}")
            # Metal. The first load on a Mac compiles its shaders (36 s once on an M1,
            # 0.1 s after that); this runs off every other thread, so she never waits.
            self.model = transcribe_cpp.Model(ggufs[0])
            self.rec = self.model.session()
            self._decode([0.0] * 16000, 16000)       # the first real turn then pays nothing extra
            self.log(f"local recogniser {self.info.get('engine')} ready in {time.time() - t:.1f}s")
        except Exception as e:  # noqa: BLE001
            self.rec = None
            self.error = f"{type(e).__name__}: {e}"
            self.log(f"local recogniser failed to load, staying on Apple: {self.error}")
        finally:
            self.loading = False
            self.last_used = time.time()
            self.loaded.set()

    @property
    def ready(self) -> bool:
        return self.rec is not None

    def transcribe(self, samples, sample_rate: int,
                   wait: float = 1.0) -> Optional[tuple[str, float]]:
        """(text, confidence 0-1) or None. confidence: the mean token probability.
        If the model is still coming back from an idle unload, waits up to `wait` s."""
        self.last_used = time.time()
        if self.rec is None and self.loading:
            self.loaded.wait(timeout=wait)
        if self.rec is None or not samples:
            return None
        try:
            return self._decode(samples, sample_rate)
        except Exception as e:  # noqa: BLE001
            self.log(f"local recogniser failed on a turn: {e!r}")
            return None

    def _decode(self, samples, sample_rate: int) -> tuple[str, float]:
        pcm = samples if int(sample_rate) == 16000 else to_16k(samples, sample_rate)
        with self.lock:
            if self.rec is None:
                raise RuntimeError("freed")
            r = self.rec.run(array("f", pcm))
        text = str(getattr(r, "text", "") or "").strip()
        conf = _confidence(r)
        return text, conf


def _confidence(result) -> float:
    """The mean of the tokens' probabilities (transcribe.cpp reports one per token);
    the listener's stand-in 0.5 when there are none."""
    probs = [float(t.p) for t in (getattr(result, "tokens", None) or ())
             if t.p == t.p]                               # NaN when a model gives none
    return sum(probs) / len(probs) if probs else 0.5


def to_16k(samples, rate: int) -> list:
    """The tap's rate (48 kHz on the owner's Mac) to the 16 kHz the model reads, with
    Apple's own converter: 37 ms for a 7-second turn. transcribe.cpp does not resample."""
    import AVFoundation as AV
    n = len(samples)
    src = AV.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AV.AVAudioPCMFormatFloat32, float(rate), 1, False)
    dst = AV.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AV.AVAudioPCMFormatFloat32, 16000.0, 1, False)
    inb = AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(src, n)
    inb.setFrameLength_(n)
    ch = inb.floatChannelData()[0]
    for i, v in enumerate(samples):
        ch[i] = v
    conv = AV.AVAudioConverter.alloc().initFromFormat_toFormat_(src, dst)
    out = AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
        dst, int(n * 16000 / rate) + 1024)
    fed = [False]

    def give(count, status):
        if fed[0]:
            return None, AV.AVAudioConverterInputStatus_EndOfStream
        fed[0] = True
        return inb, AV.AVAudioConverterInputStatus_HaveData
    conv.convertToBuffer_error_withInputFromBlock_(out, None, give)
    return list(out.floatChannelData()[0].as_tuple(out.frameLength()))

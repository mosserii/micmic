"""How MicMic sounds when it talks.

Every spoken line goes through savta/actions/mac.py say(), and from there through here:

  prepare()   What any voice reads, `say` included. Emoji and video-title clutter are
              taken out (`say` read "Masha and the Bear 💥" as "... collision symbol"),
              shouted titles are brought down to normal case, " - " and "|" become pauses.
              This cleaned text is what every engine is given, the cloud voice too.

  speak()     The first engine in this language's list (ENGINE_ORDER, overridden by
              micmic.config.json "voice_engines") that can speak now:
                eleven_turbo  ElevenLabs Eleven v4 Turbo through MicMic's cloud
                              (proxy/proxy/tts.py), streamed: the first bytes play while
                              the rest arrives. The only engine an audio tag reaches
                              (savta/voice_tags.py). Every language's default.
                kokoro        Kokoro-82M on this Mac through MLX, only once she has
                              downloaded it (Settings > Voice > On-device voice
                              (English); savta/voice_models.py). Set aside since 1.2.4:
                              English's stand-in when the cloud voice cannot speak, and
                              her voice when she picks "On-device voice". Never
                              downloaded by default. The first clause is synthesised
                              and starts playing at once; the rest while it plays.
                say           macOS `say`: False from speak(), the caller runs it.
              Settings > Voice: "cloud" (the list as it is), "device" (without the cloud
              voice: nothing she hears leaves the Mac), "mac" (`say` only).

  stop()      Cuts whichever voice is talking off mid-word, like terminating `say`.

  kept lines  A line the cloud voice has said once in full, and that
              voice_tags.PRIVATE_LINE_RULE allows (the router's `cache`), is kept as PCM
              under Application Support/MicMic/voice/cache (LRU, KEEP_MAX_BYTES). The
              next time the same words are said with the same voice, model, language
              and tag, they play from there at once: no network, no credits.

Any failure (no model, no network, no audio within FIRST_AUDIO_S, a cut stream, a 4xx or
5xx, the key not set on the server, her characters used up, an exception in the middle of
a sentence) hands whatever was not heard yet to the next engine in the list, and in the
end to `say`, at once; the cloud voice then rests (_fail(): a growing pause) instead of
failing every line. Offline it is not even tried. A line that was stopped, or replaced
by a newer one, never comes back through a fallback (_GEN): two replies never overlap.
MicMic never goes silent, nor slower than `say`, because of this file.

Pronunciation: LEXICON gives Kokoro the sounds of names and apps that the letter-to-sound
rules (espeak-ng) get wrong: MicMic was "Mike Mike", macOS "mac-oss", Strasbourg
"Strass-burg" with a flat a. Times are written out before that ("7:09 pm" was "seven zero
nine").

Tests never load a model, play a sound or reach a voice server: ENGINE_FACTORY and
PLAYER_FACTORY are the seams, and the cloud voice is pointed at a fake on 127.0.0.1
(any other host from a test is refused and leaves REAL_TTS_IN_TEST in the state folder).
"""
from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
import re
import struct
import sys
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

# ---------------------------------------------------------------- what any voice reads

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000020E3\U000E0020-\U000E007F]+")
# What YouTube and music titles carry that nobody would say out loud.
_TITLE_JUNK = re.compile(
    r"\s*[\(\[](?:official\s+)?(?:music\s+|lyric\s+)?(?:video|audio|visuali[sz]er|lyrics?|"
    r"hd|hq|4k|explicit|remastered(?:\s+\d{4})?)[\)\]]", re.I)
_CAPS_RUN = re.compile(r"\b[A-Z][A-Z']+(?:\s+[A-Z][A-Z']+)+\b")


def prepare(text: str, language: str = "english") -> str:
    """The line as any voice should read it. Never empty unless the input was."""
    t = _EMOJI.sub(" ", text or "")
    t = _TITLE_JUNK.sub("", t)
    t = re.sub(r"\s+[|•·]\s+|\s+[-–—]\s+|\s*;\s*", ", ", t)
    if language == "english":
        # "NEW EPISODE" is a title shouting, not two acronyms. A lone short capital
        # word (BBC, USA, OK) is left alone.
        t = _CAPS_RUN.sub(lambda m: m.group(0).title(), t)
    t = re.sub(r"\s+([,.!?:;])", r"\1", t)
    t = re.sub(r"([,!?:;])(?=[^\s\d,.!?:;\"')\]])", r"\1 ", t)
    t = re.sub(r",\s*,+", ",", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    return t if t else (text or "").strip()


# ---------------------------------------------------------------- English, written out

_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s?m\b\.?)?", re.I)


def _say_time(m: re.Match) -> str:
    h, mm, ap = int(m.group(1)), m.group(2), (m.group(3) or "").lower()
    if h == 0:
        h, ap = 12, ap or "a"
    suffix = {"a": " AM", "p": " PM"}.get(ap, "")
    if mm == "00":
        return f"{h}{suffix}" if suffix else f"{h} o'clock"
    if mm.startswith("0"):
        return f"{h} oh {int(mm)}{suffix}"
    return f"{h} {mm}{suffix}"


_AMPM = re.compile(r"\b(\d{1,2})\s*([ap])\.\s?m\.?(?=\W|$)", re.I)


def normalize_en(text: str) -> str:
    """Numbers the letter-to-sound rules read badly, written the way people say them."""
    t = _TIME.sub(_say_time, text)
    return _AMPM.sub(lambda m: f"{m.group(1)} {m.group(2).upper()}M", t)


# Names and apps, in Kokoro's phoneme set (misaki's: A=ei, I=ai, O=ou, W=au, Y=oi,
# T=the American flap). Matched as whole words, case-sensitive first, then any case.
LEXICON: dict[str, str] = {
    "MicMic": "mˈɪkmɪk",
    "macOS": "mˌækOˈɛs",
    "iOS": "ˌIOˈɛs",
    "iPhone": "ˈIfOn",
    "iPad": "ˈIpæd",
    "iMessage": "ˈImˌɛsəʤ",
    "iCloud": "ˈIklWd",
    "WhatsApp": "wˈʌtsˌæp",
    "FaceTime": "fˈAstˌIm",
    "YouTube": "jˈutˌub",
    "Spotify": "spˈɑTəfˌI",
    "Wi-Fi": "wˈIfˌI",
    "Siri": "sˈɪɹi",
    "Gmail": "ʤˈimˌAl",
    "Waze": "wˈAz",
    "Shakira": "ʃəkˈiɹə",
    "Strasbourg": "stɹˈɑsbʊɹɡ",
    "Zohar": "zOhˈɑɹ",
    "Nir": "nˈiɹ",
    "Tel Aviv": "tˌɛl əvˈiv",
    "Haifa": "hˈIfə",
}
_LEX_KEYS = sorted(LEXICON, key=len, reverse=True)
_LEX_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in _LEX_KEYS) + r")(?![\w-])",
                     re.I)
_LEX_LOWER = {k.lower(): v for k, v in LEXICON.items()}

# espeak-ng's IPA, mapped to Kokoro's training symbols (misaki's own table, Apache-2.0).
_E2M = sorted({
    "ʔˌn̩": "ʔn", "ʔn̩": "ʔn", "a^ɪ": "I", "a^ʊ": "W", "d^ʒ": "ʤ", "e^ɪ": "A",
    "e": "A", "t^ʃ": "ʧ", "ɔ^ɪ": "Y", "ə^l": "ᵊl", "ʲo": "jo", "ʲə": "jə", "ʲ": "",
    "ɚ": "əɹ", "r": "ɹ", "x": "k", "ç": "k", "ɐ": "ə", "ɬ": "l", "̃": "",
}.items(), key=lambda kv: -len(kv[0]))
# A pause mark only where it ends a word: "15.6.1" and "3.5" stay numbers.
_PUNCT = re.compile(r"([,.!?;:…—])(?=\s|$)")


class Espeak:
    """espeak-ng through ctypes: letters to IPA, nothing else (no audio)."""

    _lock = threading.Lock()                     # the library keeps global state

    def __init__(self, loader_dir: Path, voice: str = "en-us"):
        self.lib = ctypes.cdll.LoadLibrary(str(loader_dir / "libespeak-ng.dylib"))
        if self.lib.espeak_Initialize(0x02, 0, str(loader_dir / "espeak-ng-data").encode(), 0) <= 0:
            raise RuntimeError("espeak-ng did not start")
        self.lib.espeak_SetVoiceByName(voice.encode())
        self.lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
        self.lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_char_p),
                                                   ctypes.c_int, ctypes.c_int]

    def ipa(self, text: str) -> str:
        mode = 0x02 | 0x80 | (ord("^") << 8)     # IPA, multi-letter phonemes tied with ^
        out = []
        with self._lock:
            ptr = ctypes.pointer(ctypes.c_char_p(text.encode()))
            while ptr.contents.value:
                r = self.lib.espeak_TextToPhonemes(ptr, 1, mode)
                if r:
                    out.append(r.decode("utf-8", "ignore"))
        ps = re.sub(r"\([a-z-]+\)", "", " ".join(out))    # language-switch flags
        for old, new in _E2M:
            ps = ps.replace(old, new)
        ps = re.sub(r"(\S)̩", r"ᵊ\1", ps).replace("̩", "")
        ps = (ps.replace("o^ʊ", "O").replace("ɜːɹ", "ɜɹ").replace("ɜː", "ɜɹ")
                .replace("ɪə", "iə").replace("ː", "").replace("o", "ɔ"))
        ps = ps.replace("ɾ", "T").replace("ʔ", "t").replace("^", "")
        return re.sub(r"\s+", " ", ps).strip()


def phonemize(text: str, g2p: Callable[[str], str]) -> str:
    """Kokoro's input for one chunk: lexicon words as written in LEXICON, the rest
    through g2p, punctuation kept (it is what makes the pauses)."""
    out: list[str] = []
    for part in _PUNCT.split(normalize_en(text)):
        if not part.strip():
            continue
        if _PUNCT.fullmatch(part):
            if out:
                out[-1] = out[-1].rstrip()
            out.append(part + " ")
            continue
        pos = 0
        for m in _LEX_RE.finditer(part):
            if part[pos:m.start()].strip():
                out.append(g2p(part[pos:m.start()]) + " ")
            out.append(LEXICON.get(m.group(1)) or _LEX_LOWER[m.group(1).lower()])
            out.append(" ")
            pos = m.end()
        if part[pos:].strip():
            out.append(g2p(part[pos:]) + " ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


# ---------------------------------------------------------------- chunks

# "Mrs. O'Brien" and "9 a.m. tomorrow" are not two sentences.
_SENT_END = re.compile(r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)"
                       r"(?<!\bvs\.)(?<!\ba\.m\.)(?<!\bp\.m\.)(?<=[.!?…])\s+")
# Words a phrase starts with: the first part may end just before one of them. A verb
# only after a noun ("Microsoft apps | can be tricky"), never after a pronoun ("In
# Strasbourg it | is cloudy" was heard as a stumble).
_PHRASE_START = set("by on at in to with for from and but so because then like about of "
                    "after before into than as if when while where who which".split())
_VERB_START = set("can will would should could is are was were has have had does did".split())
_PRONOUN = set("it he she they we i you there this that what who which".split())
FIRST_WORDS = 4          # a longer first sentence is cut after its first few words
MAX_CHARS = 280          # Kokoro reads up to 510 phonemes; stay well inside


def _first_cut(first: str) -> int:
    """How many words the first part keeps (0: the whole sentence)."""
    words = first.split()
    if len(words) <= FIRST_WORDS:
        return 0
    for i in range(min(5, len(words) - 1)):
        if words[i][-1] in ",:;":
            return i + 1
    bare = [w.lower().strip("\"'(),") for w in words]
    for i in range(2, min(5, len(words))):
        if bare[i] in _PHRASE_START:
            return i
    for i in range(2, min(5, len(words))):
        if bare[i] in _VERB_START and bare[i - 1] not in _PRONOUN:
            return i
    return 0


def chunks(text: str) -> list[str]:
    """Sentences, with a long first one cut after its first two to four words: at a
    comma or colon when there is one that early ("Sending Dana: | I'm running late."),
    else where a phrase starts ("Playing Hips Don't Lie | by Shakira on Spotify").
    Synthesis time grows with length, so the first sound comes out about as fast as
    `say`'s; the rest is made while the first part plays."""
    sents = [s for s in _SENT_END.split(text.strip()) if s]
    if not sents:
        return []
    cut = _first_cut(sents[0])
    if cut:
        words = sents[0].split()
        sents[0:1] = [" ".join(words[:cut]), " ".join(words[cut:])]
    out: list[str] = []
    for s in sents:
        while len(s) > MAX_CHARS:
            cut = max(s.rfind(", ", 0, MAX_CHARS), s.rfind(" ", 0, MAX_CHARS))
            cut = cut if cut > 0 else MAX_CHARS
            out.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------- the engine

class Kokoro:
    """Kokoro-82M through mlx-audio's model code, from the downloaded runtime folder.
    Construction loads the weights and compiles the Metal kernels once (~1-2 s)."""

    sample_rate = 24000

    def __init__(self, model_dir: Path, runtime_dir: Path, voice: str = "af_heart"):
        rt = str(runtime_dir)
        if rt not in sys.path:
            sys.path.insert(0, rt)
        # mlx-audio's helpers import huggingface_hub for downloads MicMic never makes
        # through it: a stand-in that refuses keeps the 30 MB package out.
        if "huggingface_hub" not in sys.modules:
            hub = types.ModuleType("huggingface_hub")

            def _refuse(*a, **k):
                raise RuntimeError("MicMic downloads its own files")
            hub.snapshot_download = hub.hf_hub_download = _refuse
            sys.modules["huggingface_hub"] = hub
        import mlx.core as mx
        import numpy as np
        from mlx_audio.tts.models.kokoro.kokoro import Model, ModelConfig
        self.mx, self.np = mx, np
        cfg = json.loads((model_dir / "config.json").read_text())
        self.model = Model(ModelConfig.from_dict(cfg))
        weights = mx.load(str(model_dir / "kokoro-v1_0.safetensors"))
        self.model.load_weights(list(self.model.sanitize(weights).items()))
        mx.eval(self.model.parameters())
        self.pack = mx.load(str(model_dir / "voices" / f"{voice}.safetensors"))["voice"]
        self.vocab = cfg["vocab"]
        self.espeak = Espeak(runtime_dir / "espeakng_loader")
        self.lock = threading.Lock()
        self.synth("Hello.")                     # Metal compiles its kernels here, once

    def synth(self, text: str) -> bytes:
        ps = "".join(p for p in phonemize(text, self.espeak.ipa) if p in self.vocab)[:500]
        if not ps:
            return b""
        with self.lock:
            audio = self._forward(ps, self.pack[len(ps) - 1])
            a = self.np.asarray(audio, dtype=self.np.float32).reshape(-1)
        return trim(a, self.sample_rate, text, self.np).tobytes()

    def _forward(self, ps: str, ref_s):
        """mlx-audio's Model.__call__, with the frame alignment built in one vectorised
        step instead of a Python loop over phonemes (0.48 -> 0.37 s for one sentence on
        the M1). Same audio (checked 2026-10-01 against Model.__call__). The silence
        before the first word is NOT shortened here: the decoder needs those frames to
        settle, and with them cut it swallowed the first word ("Sending" was lost,
        measured with Whisper). trim() takes it off the finished audio instead."""
        mx, m = self.mx, self.model
        ids = mx.array([[0, *[self.vocab[p] for p in ps], 0]])
        n = ids.shape[-1]
        lengths = mx.array([n])
        mask = mx.zeros((1, n), dtype=mx.bool_)
        bert_dur, _ = m.bert(ids, attention_mask=mx.ones((1, n), dtype=mx.int32))
        d_en = m.bert_encoder(bert_dur).transpose(0, 2, 1)
        s = ref_s[:, 128:]
        d = m.predictor.text_encoder(d_en, s, lengths, mask)
        x, _ = m.predictor.lstm(d)
        dur = mx.sigmoid(m.predictor.duration_proj(x)).sum(axis=-1)
        dur = mx.clip(mx.round(mx.nan_to_num(dur, nan=1.0)), 1, 100).astype(mx.int32)[0]
        ends = mx.cumsum(dur)
        starts = ends - dur
        frames = mx.arange(int(ends[-1].item()))
        aln = ((frames[None, :] >= starts[:, None])
               & (frames[None, :] < ends[:, None])).astype(d.dtype)[None]
        en = d.transpose(0, 2, 1) @ aln
        f0, noise = m.predictor.F0Ntrain(en, s)
        asr = m.text_encoder(ids, lengths, mask) @ aln
        audio = m.decoder(asr, f0, noise, ref_s[:, :128])[0]
        mx.eval(audio)
        return audio


# Silence kept around each chunk, in seconds. Kokoro starts every chunk with ~0.2 s of
# silence and ends it with ~0.45 s (measured): the first was heard as MicMic being slow
# to answer, the second as a stall between the parts of one sentence.
LEAD_KEEP = 0.02
TAIL_KEEP = {"end": 0.25, "clause": 0.14, "cut": 0.03}


def trim(a, rate: int, text: str, np):
    """Cut a chunk's silence down to LEAD_KEEP before and TAIL_KEEP after, by where the
    sound is within 40 dB of the chunk's loudest 10 ms."""
    win = max(1, rate // 100)
    n = len(a) // win
    if n < 3:
        return a
    env = np.sqrt((a[: n * win].reshape(n, win) ** 2).mean(axis=1))
    loud = np.nonzero(env > env.max() * 0.01)[0]
    if not len(loud):
        return a
    end = text.rstrip()[-1:] if text.strip() else ""
    keep = TAIL_KEEP["end" if end in ".!?…" else "clause" if end in ",:;" else "cut"]
    start = max(0, loud[0] * win - int(LEAD_KEEP * rate))
    stop = min(len(a), (loud[-1] + 1) * win + int(keep * rate))
    return a[start:stop]


# ---------------------------------------------------------------- the speaker

class AudioQueuePlayer:
    """Mono PCM to the default output through AudioToolbox's AudioQueue (ctypes, no
    extra package): float32 from Kokoro, 16-bit little-endian integers ("s16") from the
    cloud voice. play() queues a buffer and starts on the first; stop() cuts it at once."""

    def __init__(self, rate: int, fmt: str = "f32"):
        AT = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
        self.AT = AT
        vp, u32 = ctypes.c_void_p, ctypes.c_uint32

        class ASBD(ctypes.Structure):
            _fields_ = [("rate", ctypes.c_double), ("fmt", ctypes.c_uint32),
                        ("flags", ctypes.c_uint32), ("bpp", ctypes.c_uint32),
                        ("fpp", ctypes.c_uint32), ("bpf", ctypes.c_uint32),
                        ("ch", ctypes.c_uint32), ("bits", ctypes.c_uint32),
                        ("res", ctypes.c_uint32)]

        class Buf(ctypes.Structure):
            _fields_ = [("cap", ctypes.c_uint32), ("data", ctypes.c_void_p),
                        ("size", ctypes.c_uint32), ("user", ctypes.c_void_p),
                        ("pdcap", ctypes.c_uint32), ("pd", ctypes.c_void_p),
                        ("pdcount", ctypes.c_uint32)]
        self.Buf = Buf
        CB = ctypes.CFUNCTYPE(None, vp, vp, ctypes.POINTER(Buf))
        for name, args in (("AudioQueueNewOutput", [vp, CB, vp, vp, vp, u32, vp]),
                           ("AudioQueueAllocateBuffer", [vp, u32, vp]),
                           ("AudioQueueEnqueueBuffer", [vp, ctypes.POINTER(Buf), u32, vp]),
                           ("AudioQueueStart", [vp, vp]),
                           ("AudioQueueStop", [vp, ctypes.c_bool]),
                           ("AudioQueueDispose", [vp, ctypes.c_bool])):
            getattr(AT, name).argtypes = args
            getattr(AT, name).restype = ctypes.c_int32
        self.cv = threading.Condition()
        self.lock = threading.Lock()              # stop() and close() never interleave
        self.pending = 0
        self.started = False
        self.closed = False

        def done(_user, _q, _buf):
            # The buffer stays with the queue; AudioQueueDispose frees them all.
            with self.cv:
                self.pending -= 1
                self.cv.notify_all()
        self._cb = CB(done)                       # kept alive as long as the queue
        lpcm = int.from_bytes(b"lpcm", "big")
        if fmt == "s16":     # kAudioFormatFlagIsSignedInteger | IsPacked, 2 bytes a frame
            desc = ASBD(float(rate), lpcm, 4 | 8, 2, 1, 2, 1, 16, 0)
        else:                # kAudioFormatFlagIsFloat | IsPacked, 4 bytes a frame
            desc = ASBD(float(rate), lpcm, 1 | 8, 4, 1, 4, 1, 32, 0)
        self.q = ctypes.c_void_p()
        err = AT.AudioQueueNewOutput(ctypes.byref(desc), self._cb, None, None, None, 0,
                                     ctypes.byref(self.q))
        if err:
            raise OSError(f"AudioQueueNewOutput {err}")

    def play(self, pcm: bytes) -> None:
        with self.lock:
            if pcm and not self.closed:
                self._play(pcm)

    def _play(self, pcm: bytes) -> None:
        ref = ctypes.POINTER(self.Buf)()
        err = self.AT.AudioQueueAllocateBuffer(self.q, ctypes.c_uint32(len(pcm)),
                                               ctypes.byref(ref))
        if err:
            raise OSError(f"AudioQueueAllocateBuffer {err}")
        ctypes.memmove(ref.contents.data, pcm, len(pcm))
        ref.contents.size = len(pcm)
        with self.cv:
            self.pending += 1
        err = self.AT.AudioQueueEnqueueBuffer(self.q, ref, 0, None)
        if err:
            raise OSError(f"AudioQueueEnqueueBuffer {err}")
        if not self.started:
            self.started = True
            err = self.AT.AudioQueueStart(self.q, None)
            if err:
                raise OSError(f"AudioQueueStart {err}")

    def drain(self, cancel: threading.Event) -> None:
        with self.cv:
            while self.pending > 0 and not cancel.is_set():
                self.cv.wait(0.05)

    def stop(self) -> None:
        with self.lock:
            if not self.closed:
                self.AT.AudioQueueStop(self.q, True)
        with self.cv:
            self.pending = 0
            self.cv.notify_all()

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.closed = True
                self.AT.AudioQueueStop(self.q, True)
                self.AT.AudioQueueDispose(self.q, True)


# The seams: a test swaps these for fakes and nothing is loaded or heard.
ENGINE_FACTORY: Callable[[str], object] = lambda lang: _load_kokoro(lang)  # noqa: E731
PLAYER_FACTORY: Callable[..., object] = AudioQueuePlayer      # (rate, fmt="f32"|"s16")

_TAIL = 0.12             # seconds of silence after the last chunk: the hardware's last
                         # few milliseconds are not cut off when the queue stops


def _load_kokoro(lang: str) -> Kokoro:
    from . import voice_models as vm
    return Kokoro(vm.model_dir(lang), vm.runtime_dir(), vm.MODELS[lang]["voice"])


class _Utterance:
    def __init__(self, text: str, engine, fallback: Callable[[str], None]):
        self.text, self.engine, self.fallback = text, engine, fallback
        self.cancel = threading.Event()
        self.player = None
        self.first_audio: Optional[float] = None
        self.t0 = time.perf_counter()
        self.thread = threading.Thread(target=self._run, daemon=True, name="micmic-voice")

    def _run(self) -> None:
        parts = chunks(self.text)
        spoken = 0
        try:
            rate = getattr(self.engine, "sample_rate", 24000)
            for i, part in enumerate(parts):
                if self.cancel.is_set():
                    return
                pcm = _cached(self.engine, part)
                if self.cancel.is_set():
                    return
                if i == len(parts) - 1:
                    pcm += b"\0" * (4 * int(rate * _TAIL))
                if self.player is None:
                    self.player = PLAYER_FACTORY(rate)
                self.player.play(pcm)
                spoken = i + 1
                if self.first_audio is None:
                    self.first_audio = time.perf_counter() - self.t0
                    STATS.append(self.first_audio)
            if self.player is not None:
                self.player.drain(self.cancel)
        except Exception as e:  # noqa: BLE001
            LAST_ERROR[0] = f"{type(e).__name__}: {e}"[:200]
            if not self.cancel.is_set():
                rest = " ".join(parts[spoken:]).strip()
                if rest:
                    _hand_on(self, rest, self.fallback)
        finally:
            if self.player is not None:
                try:
                    self.player.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self.cancel.set()
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass


_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_CACHE_MAX = 48          # recent chunks; "Opening WhatsApp." and friends come back often


def _cached(engine, part: str) -> bytes:
    key = f"{id(engine)}:{part}"
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    pcm = engine.synth(part)
    _CACHE[key] = pcm
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return pcm


# First parts MicMic's answers often start with, made once when the voice loads.
WARM_PHRASES = {"english": ("Okay.", "Sure.", "Done.", "Here you go.", "Got it.", "Oops,",
                            "Sorry,", "One moment.", "Something went wrong on my side.")}
_STATE = {"engine": None, "lang": None, "loading": False, "current": None}
_STATE_LOCK = threading.Lock()
# Every stop() (so every new line) bumps the generation. A line's fallback runs only
# while that line is still the current one, and under _HAND_LOCK, which stop() takes
# too: an old line that failed late can never talk over, or cut off, a newer one, and
# stop() always finds (and stops) a fallback that has just started.
_GEN = [0]
_HAND_LOCK = threading.RLock()


def _hand_on(u, rest: str, to: Callable[[str], None]) -> None:
    """Give what `u` could not say to the next voice, unless `u` was stopped or replaced."""
    with _HAND_LOCK:
        if u.cancel.is_set() or getattr(u, "gen", None) != _GEN[0]:
            return
        try:
            to(rest)
        except Exception:  # noqa: BLE001
            pass
STATS: list = []                 # Kokoro: seconds to first audio, newest last (diagnostics)
CLOUD_STATS: list = []           # the cloud voice: the same, measured in this process
LAST_ERROR = [""]


# ---------------------------------------------------------------- which engine speaks

# Per language, the engines to try in order. The first that can speak a line now does;
# one that fails halfway hands what was not heard to the next one. micmic.config.json
# "voice_engines" overrides this table language by language.
#   eleven_turbo  ElevenLabs Eleven v4 Turbo through MicMic's cloud (or her own key in a
#                 source build). The only engine that hears an audio tag. Every language.
#   kokoro        Kokoro-82M on this Mac, ONLY if she downloaded it (savta/voice_models.py,
#                 Settings > Voice). English only. Until then it is skipped, so a new
#                 user's English is [eleven_turbo, say] like every other language.
#   say           macOS `say`, always there; every list ends with it.
ENGINE_ORDER: dict[str, tuple[str, ...]] = {
    "english": ("eleven_turbo", "kokoro", "say"),
    "hebrew": ("eleven_turbo", "say"),
    "arabic": ("eleven_turbo", "say"),
    "russian": ("eleven_turbo", "say"),
}
ENGINES = ("eleven_turbo", "kokoro", "say")
CLOUD_ENGINES = frozenset({"eleven_turbo"})
# Settings > Voice. "cloud": the table above as it is. "device": the same without the
# cloud voice, so nothing she hears leaves the Mac. "mac": macOS `say` only.
CHOICES = ("cloud", "device", "mac")


def _config() -> dict:
    try:
        from .router import load_config
        return load_config()
    except Exception:  # noqa: BLE001
        return {}


def _chosen_language() -> str:
    try:
        from .router import chosen_language
        return chosen_language()
    except Exception:  # noqa: BLE001
        return "english"


def choice(cfg: dict | None = None) -> str:
    v = (cfg if cfg is not None else _config()).get("voice", "cloud")
    if v == "natural":                 # the first prototype's name for the voice on the Mac
        return "cloud"
    return v if v in CHOICES else "cloud"


def engines(language: str, cfg: dict | None = None) -> list[str]:
    """The engines that may speak this language, best first, after her choice."""
    cfg = cfg if cfg is not None else _config()
    table = cfg.get("voice_engines")
    order = (table.get(language) if isinstance(table, dict) else None) \
        or ENGINE_ORDER.get(language) or ("say",)
    order = [e for e in order if e in ENGINES]
    c = choice(cfg)
    if c == "mac":
        order = []
    elif c == "device":
        order = [e for e in order if e not in CLOUD_ENGINES]
    if "say" in order:
        order = order[:order.index("say")]
    return order + ["say"]


def _kokoro_engine(language: str):
    return _STATE["engine"] if _STATE["lang"] == language else None


def plan(language: str) -> str:
    """The engine that would speak a line in this language right now. The router asks
    this before choosing a tag: only eleven_turbo ever gets one."""
    for e in engines(language):
        if e == "eleven_turbo" and language in LANG_CODES and cloud_route():
            return e
        if e == "kokoro" and _kokoro_engine(language) is not None:
            return e
        if e == "say":
            break
    return "say"


def wanted(language: str = "english") -> bool:
    """Kokoro is in this language's list, after her choice."""
    return "kokoro" in engines(language)


def _available(lang: str) -> bool:
    from . import voice_models as vm
    return lang in vm.MODELS and vm.supported() and vm.is_ready(lang)


def warm(lang: str | None = None, force: bool = False) -> None:
    """Load Kokoro in the background when this language may use it and it is installed.
    The server calls this at start, after Settings change, and when a download finishes.
    Where Kokoro only stands in for the cloud voice it is loaded the first time the
    cloud voice fails (force), not before: it holds a few hundred MB."""
    lang = lang or _chosen_language()
    order = engines(lang)
    if "kokoro" not in order or not _available(lang):
        return
    if (not force and order[0] in CLOUD_ENGINES and order.index("kokoro") > 0
            and cloud_route() is not None):
        return
    with _STATE_LOCK:
        if _STATE["loading"] or (_STATE["engine"] is not None and _STATE["lang"] == lang):
            return
        _STATE["loading"] = True

    def load():
        try:
            eng = ENGINE_FACTORY(lang)
            with _STATE_LOCK:
                _STATE["engine"], _STATE["lang"] = eng, lang
            for phrase in WARM_PHRASES.get(lang, ()):
                _cached(eng, phrase)              # these start many answers: no wait at all
        except Exception as e:  # noqa: BLE001
            LAST_ERROR[0] = f"load {type(e).__name__}: {e}"[:200]
        finally:
            with _STATE_LOCK:
                _STATE["loading"] = False
    threading.Thread(target=load, daemon=True, name="micmic-voice-load").start()


def speak(text: str, language: str, fallback: Callable[[str], None],
          tag: str | None = None, cache: bool = False, lead: str | None = None) -> bool:
    """Say `text` without waiting, with the first engine in this language's list that can.
    False when that is `say`: the caller speaks it as every line was spoken before.
    `fallback` speaks whatever no engine could. `tag` reaches the cloud voice only;
    `lead` (a prefix of `text`, e.g. "Dana:") puts the tag after it instead of in front,
    so a friend's message carries the friend's tone and MicMic's own words stay plain.
    `cache` (set by the router only when voice_tags.PRIVATE_LINE_RULE allows it, never
    for her messages, notes, screen, or a line with a name or her words in it) lets
    this Mac keep the line's audio (a repeat plays from it, with no network) and
    MicMic's cloud keep it too."""
    if lead and not (tag and text.startswith(lead) and text[len(lead):].strip()):
        lead = None
    return _start(text, language, engines(language), fallback, tag, cache, lead)


def _start(text: str, language: str, order: list[str], fallback, tag, cache,
           lead=None) -> bool:
    for i, e in enumerate(order):
        if e == "eleven_turbo":
            after = order[i + 1:]
            keep = keep_key(text, language, tag, lead) if cache else None
            pcm = kept(keep) if keep else None
            if pcm:
                # Said in full before: from this Mac, at once. Offline too.
                _begin(_KeptUtterance(text, pcm, lambda rest: _after_cloud(
                    rest, language, after, fallback)))
                return True
            route = cloud_route() if language in LANG_CODES else None
            if route is None or _cloud_chars(text, tag) > MAX_CLOUD_CHARS:
                continue
            _begin(_CloudUtterance(text, language, tag, cache, route,
                                   lambda rest: _after_cloud(rest, language, after, fallback),
                                   lead, keep))
            return True
        if e == "kokoro":
            eng = _kokoro_engine(language)
            if eng is None:
                warm(language)                   # this line is the next engine's
                continue
            _begin(_Utterance(text, eng, fallback))
            return True
        if e == "say":
            return False
    return False


def _begin(u) -> None:
    with _HAND_LOCK:
        stop()
        u.gen = _GEN[0]
        with _STATE_LOCK:
            _STATE["current"] = u
    u.thread.start()


def _after_cloud(rest: str, language: str, after: list[str], fallback) -> None:
    """The cloud voice could not say `rest`: the next engine in the list says it."""
    warm(language, force=True)
    if not _start(rest, language, after, fallback, None, False):
        fallback(rest)


def busy() -> bool:
    u = _STATE["current"]
    return bool(u is not None and u.thread.is_alive() and not u.cancel.is_set()
                and getattr(u, "state", "") != "gave_up")


def stop() -> bool:
    """Cut the voice off, Kokoro's or the cloud's. True if it was speaking."""
    with _HAND_LOCK:
        _GEN[0] += 1
        with _STATE_LOCK:
            u, _STATE["current"] = _STATE["current"], None
    if u is None:
        return False
    was = u.thread.is_alive() and not u.cancel.is_set()
    u.stop()
    return was


def once_today(key: str, day_from: int = 5) -> bool:
    """True the first time this is asked for `key` on her local day (which starts at
    `day_from` o'clock), False after that until tomorrow. The date is kept in the state
    folder (voice_day.json), so a restart does not repeat a good morning."""
    now = time.localtime()
    if now.tm_hour < day_from:
        return False
    today = time.strftime("%Y-%m-%d", now)
    try:
        from .paths import state
        f = state("voice_day.json")
    except Exception:  # noqa: BLE001
        return False
    with _STATE_LOCK:
        try:
            d = json.loads(f.read_text())
            d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            d = {}
        if d.get(key) == today:
            return False
        d[key] = today
        try:
            f.write_text(json.dumps(d))
        except OSError:
            return False
    return True


def _p50(xs: list) -> float | None:
    s = sorted(xs[-50:])
    return round(s[len(s) // 2], 3) if s else None


def status() -> dict:
    """For /api/voice: her choice, which engine speaks now, what is loaded, and how fast
    the last lines started. Never the token or a key."""
    lang = _chosen_language()
    route = cloud_route(ignore_pause=True)
    paused = _COOL["why"] if time.time() < _COOL["until"] else ""
    online = route is None or _online(_endpoint(route[1])[1])
    listed = engines(lang, {**_config(), "voice": "cloud"})     # before her choice
    return {"choice": choice(), "language": lang, "engines": engines(lang),
            "plan": plan(lang), "wanted": wanted(lang),
            # What Settings > Voice offers for her language.
            "offers_cloud": any(e in CLOUD_ENGINES for e in listed),
            "offers_kokoro": "kokoro" in listed,
            "cloud": {"available": route is not None and not paused and online,
                      "via": route[0] if route else None, "paused": paused or None,
                      "offline": not online, "first_audio_p50": _p50(CLOUD_STATS)},
            "loaded": _STATE["engine"] is not None, "loading": _STATE["loading"],
            "kept": dict(KEEP_STATS),
            "error": LAST_ERROR[0], "first_audio_p50": _p50(STATS)}


# ---------------------------------------------------------------- the cloud voice

# ElevenLabs Eleven v4 Turbo, voice Jessica, 24 kHz 16-bit PCM (micmic-work/voice/
# ELEVENLABS_V4.md: first byte p50 361 ms direct from this Mac, the best Hebrew numbers
# and dates of every engine measured). Through MicMic's cloud the key stays on the
# server: the app never holds one, and never sends one. With her own ELEVENLABS_API_KEY
# (a source build's .env.local) a source build talks to ElevenLabs directly, the way her
# own Jev key does.
CLOUD_MODEL = "eleven_v4_turbo"
CLOUD_VOICE = ("jessica", "cgSgspJ2msm6clMCkdW9")
CLOUD_RATE = 24000
ELEVEN_API = "https://api.elevenlabs.io"
LANG_CODES = {"english": "en", "hebrew": "he", "arabic": "ar", "russian": "ru"}
# No audio by then and the line goes to the next engine: a watchdog (not the socket's
# own timeout, which a slow DNS lookup or TLS handshake does not respect) hands it on.
# The proxy hop is about a tenth of a second over the 0.36 s measured direct.
FIRST_AUDIO_S = 1.5
STALL_S = 2.0            # silence in the middle of a stream for this long is a failure
CHARS_PER_S = 14.0       # how far a cut-off line got, to hand on only what was not heard
IDLE_FRESH = 40.0        # a kept-alive connection older than this is treated as closed
MAX_CLOUD_CHARS = 600    # the proxy's TTS_MAX_CHARS: a longer line is not sent at all
# How long the cloud voice rests after the 1st, 2nd, 3rd, 4th+ failure in a row (no
# audio in time, a cut stream, a 4xx/5xx, unreachable). One success resets it. A refusal
# that says how long (her limit, the server switched off, no key) sets its own rest.
BACKOFF_S = (10.0, 60.0, 300.0, 900.0)

_COOL = {"until": 0.0, "why": "", "fails": 0}
_CONN = {"key": None, "conn": None, "at": 0.0}
_CONN_LOCK = threading.Lock()
TEST_TRIPWIRE = "REAL_TTS_IN_TEST"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _own_eleven_key() -> str | None:
    """Her own ElevenLabs key: the environment, or .env.local in a source checkout. A
    test (MICMIC_STATE_DIR) never reads the owner's .env.local. The release build has no
    .env.local (native/build.sh removes it), so it always goes through MicMic's cloud."""
    k = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if k:
        return k
    if os.environ.get("MICMIC_STATE_DIR"):
        return None
    from .jev import _env_local
    return _env_local("ELEVENLABS_API_KEY")


# ---------------------------------------------------------------- is there a network?

# The seam: a test sets this to fake being on or offline (host -> bool).
ONLINE_PROBE: Optional[Callable[[str], bool]] = None
_NET = {"at": -10.0, "up": True, "ref": None}
_NET_LOCK = threading.Lock()


def _online(host: str) -> bool:
    """False only when this Mac has no route to the internet at all: then `say` speaks
    at once, without a connection attempt that could wait on DNS. Unknown counts as
    online (the watchdog still bounds the wait)."""
    if ONLINE_PROBE is not None:
        return bool(ONLINE_PROBE(host))
    if host in _LOOPBACK:
        return True
    with _NET_LOCK:
        now = time.monotonic()
        if now - _NET["at"] < 1.0:
            return _NET["up"]
        up = _reachable()
        _NET.update(at=now, up=up)
        return up


def _reachable() -> bool:
    """SystemConfiguration's reachability of 0.0.0.0 (the default route), the check macOS
    apps use for "is there a network": no packet, no DNS, microseconds. Called with
    _NET_LOCK held."""
    try:
        sc = _NET.get("sc")
        if sc is None:
            sc = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/SystemConfiguration."
                                         "framework/SystemConfiguration")

            class _Addr(ctypes.Structure):
                _fields_ = [("len", ctypes.c_uint8), ("family", ctypes.c_uint8),
                            ("port", ctypes.c_uint16), ("addr", ctypes.c_uint32),
                            ("zero", ctypes.c_char * 8)]
            sc.SCNetworkReachabilityCreateWithAddress.restype = ctypes.c_void_p
            sc.SCNetworkReachabilityCreateWithAddress.argtypes = [ctypes.c_void_p,
                                                                  ctypes.POINTER(_Addr)]
            sc.SCNetworkReachabilityGetFlags.restype = ctypes.c_bool
            sc.SCNetworkReachabilityGetFlags.argtypes = [ctypes.c_void_p,
                                                         ctypes.POINTER(ctypes.c_uint32)]
            a = _Addr(16, 2, 0, 0, b"\0" * 8)                  # AF_INET, 0.0.0.0
            ref = sc.SCNetworkReachabilityCreateWithAddress(None, ctypes.byref(a))
            if not ref:
                return True
            _NET.update(sc=sc, ref=ref)
        flags = ctypes.c_uint32(0)
        if not sc.SCNetworkReachabilityGetFlags(_NET["ref"], ctypes.byref(flags)):
            return True
        reachable, needs_connection = flags.value & 0x2, flags.value & 0x4
        return bool(reachable) and not needs_connection
    except Exception:  # noqa: BLE001
        return True


def cloud_route(ignore_pause: bool = False) -> tuple[str, str, str] | None:
    """("direct", ElevenLabs URL, her key) or ("proxy", MicMic's cloud, device token),
    or None: switched off, resting after a failure, offline, or no way to reach it.
    Someone who brought her own Jev key and no ElevenLabs key is never sent through our
    cloud."""
    if os.environ.get("MICMIC_CLOUD_VOICE", "").strip().lower() in ("0", "off", "no", "false"):
        return None
    if not ignore_pause and time.time() < _COOL["until"]:
        return None
    key = _own_eleven_key()
    if key:
        route = ("direct", (os.environ.get("MICMIC_ELEVEN_URL") or ELEVEN_API).rstrip("/"), key)
    else:
        from . import jev
        creds = jev.proxy_credentials() if jev.mode() == "proxy" else None
        if not creds:
            return None
        route = ("proxy", creds[0], creds[1])
    if not ignore_pause and not _online(_endpoint(route[1])[1]):
        return None
    return route


def _pause(seconds: float, why: str) -> None:
    """Rest the cloud voice. Never shortens a longer rest already set (a month's limit
    is not cut to ten seconds by a timeout that follows it)."""
    until = time.time() + max(1.0, seconds)
    if until > _COOL["until"] or time.time() >= _COOL["until"]:
        _COOL.update(until=until, why=why)


def _fail(why: str) -> None:
    """One more failure in a row: rest for the next step of BACKOFF_S."""
    _COOL["fails"] += 1
    _pause(BACKOFF_S[min(_COOL["fails"], len(BACKOFF_S)) - 1], why)


def _cloud_chars(text: str, tag: str | None) -> int:
    return len(text) + (len(tag) + 3 if tag else 0)


def _endpoint(base: str) -> tuple[bool, str, int, str]:
    u = urlsplit(base)
    https = u.scheme == "https"
    return https, u.hostname or "", u.port or (443 if https else 80), u.path.rstrip("/")


def _guard(host: str) -> None:
    """A test (MICMIC_STATE_DIR) that reached for a real voice server: refused, with a
    tripwire the suite checks. Tests use a fake on 127.0.0.1."""
    if os.environ.get("MICMIC_STATE_DIR") and host not in _LOOPBACK:
        try:
            from .paths import state_dir
            (state_dir() / TEST_TRIPWIRE).write_text(host)
        except Exception:  # noqa: BLE001
            pass
        raise ConnectionRefusedError("a test tried to reach a real voice server")


def _take_conn(base: str, timeout: float):
    https, host, port, _ = _endpoint(base)
    _guard(host)
    key = (https, host, port)
    with _CONN_LOCK:
        c, old = None, None
        if _CONN["conn"] is not None:
            if _CONN["key"] == key and time.monotonic() - _CONN["at"] < IDLE_FRESH:
                c = _CONN["conn"]
            else:
                old = _CONN["conn"]
        _CONN.update(conn=None, key=None)
    if old is not None:
        old.close()
    if c is not None:
        return c, True
    cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    return cls(host, port, timeout=timeout), False


def _give_back(base: str, conn) -> None:
    https, host, port, _ = _endpoint(base)
    with _CONN_LOCK:
        old = _CONN["conn"]
        _CONN.update(key=(https, host, port), conn=conn, at=time.monotonic())
    if old is not None and old is not conn:
        old.close()


def prewarm(language: str | None = None) -> None:
    """Open the connection to the voice's server while Jev is still deciding, so the
    reply does not wait for a TLS handshake. No request is made: nothing is metered."""
    lang = language or _chosen_language()
    if plan(lang) != "eleven_turbo":
        return
    route = cloud_route()
    if route is None:
        return

    def go():
        try:
            conn, reused = _take_conn(route[1], 5.0)
            if not reused:
                conn.connect()
            _give_back(route[1], conn)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=go, daemon=True, name="micmic-voice-warm").start()


def _request(route, text: str, language: str, tag: str | None, cache: bool,
             lead: str | None = None):
    """(path, body, headers) for one line. Through MicMic's cloud the body names the
    voice and the tag; only a source build with her own key sends a key, and only to
    ElevenLabs itself."""
    kind, base, secret = route
    prefix = _endpoint(base)[3]
    body_text = text[len(lead):].strip() if (lead and tag) else text
    if kind == "proxy":
        body = {"text": body_text, "lang": LANG_CODES[language], "voice": CLOUD_VOICE[0],
                "cache": bool(cache)}
        if tag:
            body["tag"] = tag
            if lead:
                body["lead"] = lead       # the tag goes after these words, not before
        return (f"{prefix}/v1/tts", json.dumps(body, ensure_ascii=False).encode(),
                {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
    if tag and lead:
        said = f"{lead} [{tag}] {body_text}"
    else:
        said = f"[{tag}] {text}" if tag else text
    body = {"text": said, "model_id": CLOUD_MODEL, "language_code": LANG_CODES[language]}
    return (f"{prefix}/v1/text-to-speech/{CLOUD_VOICE[1]}/stream?output_format=pcm_24000",
            json.dumps(body, ensure_ascii=False).encode(),
            {"xi-api-key": secret, "Content-Type": "application/json"})


class _Refused(Exception):
    def __init__(self, msg: str, status: int = 0, rested: bool = False):
        super().__init__(msg)
        self.status = status        # 0: the server answered 200 with no audio
        self.rested = rested        # the refusal set its own rest (no backoff step)


def _refused(resp, route) -> None:
    """A non-200 from the voice's server. When the answer says how long retrying cannot
    help (her characters are used up, the voice is switched off or has no key on the
    server, the token is dead), the cloud voice rests that long; anything else is one
    more failure in a row (_fail)."""
    try:
        d = json.loads(resp.read(4096) or b"{}")
    except Exception:  # noqa: BLE001
        d = {}
    err = d.get("error") if isinstance(d, dict) else None
    st = resp.status
    rested = True
    if st == 429 and err == "tts_limit":
        until = _iso_seconds(d.get("resets_at"))
        _pause(until if until else 24 * 3600, "limit")
    elif st == 503 and err == "tts_disabled":
        try:
            _pause(float(resp.getheader("Retry-After") or 600), "off")
        except ValueError:
            _pause(600, "off")
    elif (st == 503 and err == "not_configured") or st == 404:
        # The key is not set on the server ("voice not configured"), or the server
        # predates the voice (an app newer than its cloud): asking again for every line
        # would only add a round trip before the voice on the Mac speaks.
        _pause(3600, "off")
    elif st == 401 and route[0] == "proxy":
        from . import jev
        jev.revoked(route[2])
        _pause(60, "account")
    elif st in (401, 402, 403) and route[0] == "direct":
        _pause(3600, "key")
    else:
        rested = False
    raise _Refused(f"voice server answered {st} {err or ''}".strip(), st, rested)


def _iso_seconds(iso) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return max(60.0, t.timestamp() - time.time())
    except ValueError:
        return None


def rest_after(text: str, heard_s: float) -> str:
    """What is left of `text` after heard_s seconds of it: from the first sentence not
    heard to its end, so a cut-off line is finished, never skipped (a few words may be
    said twice)."""
    if heard_s <= 0:
        return text
    heard = heard_s * CHARS_PER_S
    sents = [x for x in _SENT_END.split(text.strip()) if x]
    pos = 0
    for i, x in enumerate(sents):
        pos += len(x) + 1
        if pos > heard:
            return " ".join(sents[i:])
    return ""


class _CloudUtterance:
    """One line streamed from the cloud voice into the speaker as it arrives.

    state: "waiting" until the first audio, then "playing"; "gave_up" when the
    watchdog or a failure handed the line on. Exactly one of the worker and the
    watchdog moves it out of "waiting", under self.lock, so a line is either played by
    the cloud voice or handed on, never both and never neither."""

    def __init__(self, text: str, language: str, tag, cache: bool, route, on_fail,
                 lead: str | None = None, keep: str | None = None):
        self.text, self.language, self.tag, self.cache = text, language, tag, cache
        self.route, self.on_fail, self.lead = route, on_fail, lead
        # keep_key() when this line may be kept on the Mac once it has arrived in full.
        self.keep = keep
        self.got = bytearray() if keep else None
        self.cancel = threading.Event()
        self.player = None
        self.first_audio: Optional[float] = None
        self.t0 = time.perf_counter()
        self.lock = threading.Lock()
        self.state = "waiting"
        self.sock = None
        self.watchdog: Optional[threading.Timer] = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="micmic-voice-cloud")

    def _left(self) -> float:
        return self.t0 + FIRST_AUDIO_S - time.perf_counter()

    def _deadline(self) -> None:
        """FIRST_AUDIO_S passed with no audio, wherever the worker is stuck (DNS, TLS,
        a server that never answers): the next voice says the whole line now."""
        with self.lock:
            if self.state != "waiting" or self.cancel.is_set():
                return
            self.state = "gave_up"
        LAST_ERROR[0] = "cloud TimeoutError: no audio in time"
        _fail("slow")
        self._unblock()
        _hand_on(self, self.text, self.on_fail)

    def _unblock(self) -> None:
        s = self.sock
        if s is not None:
            try:
                s.shutdown(2)
            except OSError:
                pass

    def _open(self):
        path, body, headers = _request(self.route, self.text, self.language, self.tag,
                                       self.cache, self.lead)
        for attempt in (0, 1):
            left = self._left()
            if left <= 0:
                raise TimeoutError("no audio in time")
            conn, reused = _take_conn(self.route[1], left)
            try:
                conn.timeout = left
                if conn.sock is None:
                    conn.connect()
                conn.sock.settimeout(left)
                self.sock = conn.sock            # kept: getresponse() may drop it
                if self.state != "waiting" or self.cancel.is_set():
                    raise TimeoutError("given up")
                conn.request("POST", path, body=body, headers=headers)
                return conn, self.sock, conn.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                conn.close()
                if reused and attempt == 0 and self.state == "waiting":
                    continue                     # the far end closed it while idle
                raise
            except BaseException:
                conn.close()
                raise
        raise ConnectionError("voice server unreachable")

    def _gone(self) -> bool:
        return self.cancel.is_set() or self.state == "gave_up"

    def _run(self) -> None:
        self.watchdog = threading.Timer(max(0.0, self._left()), self._deadline)
        self.watchdog.daemon = True
        self.watchdog.start()
        played, conn, done = 0, None, False
        try:
            conn, sock, resp = self._open()
            if self._gone():
                return
            if resp.status != 200:
                _refused(resp, self.route)
            buf = b""
            while not self._gone():
                wait = self._left() if not played else STALL_S
                if wait <= 0:
                    raise TimeoutError("no audio in time")
                if sock is not None:
                    sock.settimeout(wait)
                b = resp.read1(16384)
                if not b:
                    break
                buf += b
                n = len(buf) - len(buf) % 2          # whole 16-bit samples only
                if not n:
                    continue
                if not played:
                    with self.lock:                  # the watchdog may have won
                        if self.state != "waiting" or self.cancel.is_set():
                            return
                        self.state = "playing"
                    self.watchdog.cancel()
                    self.player = PLAYER_FACTORY(CLOUD_RATE, "s16")
                    self.first_audio = time.perf_counter() - self.t0
                    CLOUD_STATS.append(self.first_audio)
                if self.cancel.is_set():
                    return
                self.player.play(buf[:n])
                if self.got is not None:
                    self.got += buf[:n]
                buf = buf[n:]
                played += n
            if self._gone():
                return
            if resp.length:                          # a fixed-length body cut short
                raise http.client.IncompleteRead(b"", resp.length)
            if not played:
                raise _Refused("no audio", 0)
            done = True
            _COOL["fails"] = 0
            if self.keep and not self.cancel.is_set():
                put_kept(self.keep, bytes(self.got))   # in full, and never stopped
            self.player.play(b"\0" * (2 * int(CLOUD_RATE * _TAIL)))
            self.sock = None                         # stop() must not cut a pooled socket
            if not resp.will_close:
                _give_back(self.route[1], conn)
                conn = None
            self.player.drain(self.cancel)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                mine = self.state != "gave_up"       # else the watchdog handed it on
                if self.state == "waiting":
                    self.state = "gave_up"
            if conn is not None:
                conn.close()
                conn = None
            if not mine or self.cancel.is_set() or done:
                return
            LAST_ERROR[0] = f"cloud {type(e).__name__}: {e}"[:200]
            if not (isinstance(e, _Refused) and e.rested):
                _fail("unreachable")
            if self.player is not None:
                self.player.drain(self.cancel)       # what did arrive is heard first
            rest = rest_after(self.text, played / (2 * CLOUD_RATE))
            if rest:
                _hand_on(self, rest, self.on_fail)
        finally:
            if self.watchdog is not None:
                self.watchdog.cancel()
            if conn is not None:
                conn.close()
            if self.player is not None:
                try:
                    self.player.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self.cancel.set()
        if self.watchdog is not None:
            self.watchdog.cancel()
        self._unblock()
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- lines kept on this Mac

# What the cloud voice said in full, kept as 24 kHz 16-bit PCM so a repeat ("Turning it
# up", "Who should I send it to?") plays at once with no network and no credits. Only
# lines voice_tags.PRIVATE_LINE_RULE allows reach here (the router's `cache`): never her
# messages, a message read aloud, her screen, a name or her own words.
#   key    sha256 of engine, model, voice, language, tag, lead and the cleaned text
#   file   <key>.pcm: MAGIC, format version, rate, length, sha256 of the audio (first
#          16 bytes), then the audio. Written to <key>.tmp and renamed into place, so a
#          crash leaves the old file or none, never half of one. A file that does not
#          check out is a miss (and is written over by the next good render).
#   LRU    a hit touches the file; past KEEP_MAX_BYTES the least recently heard go first.
#          MicMic deletes nothing (tests/test_micmic.py rule 9): an evicted entry is
#          renamed onto one spare file in this folder (EVICTED), which the next eviction
#          replaces. Only files this cache wrote, in this folder, ever move.
KEEP_MAX_BYTES = 50 * 1024 * 1024
KEEP_MAX_CHARS = 300          # a longer line is unlikely to come back word for word
KEEP_MAX_ENTRY = 4 * 1024 * 1024    # ~87 s of audio: anything longer is not a template
KEEP_MAGIC = b"MMVC"
KEEP_HEAD = struct.Struct("<4sBII16s")
EVICTED = "evicted.spare"
KEEP_STATS = {"hits": 0, "stored": 0, "ignored": 0}
_KEEP_LOCK = threading.Lock()


def keep_dir() -> Path:
    from . import voice_models as vm
    d = vm.base() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def keep_key(text: str, language: str, tag: str | None = None,
             lead: str | None = None) -> str | None:
    """The cache key of one cloud line, or None when it is not one to keep (a friend's
    message after "Dana:" is never kept; too long; a language the voice lacks)."""
    if lead or not text or len(text) > KEEP_MAX_CHARS or language not in LANG_CODES:
        return None
    if os.environ.get("MICMIC_CLOUD_VOICE", "").strip().lower() in ("0", "off", "no", "false"):
        return None
    raw = "\x1f".join(("eleven_turbo", CLOUD_MODEL, CLOUD_VOICE[0], LANG_CODES[language],
                       tag or "", lead or "", text))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def kept(key: str) -> bytes | None:
    """The audio kept for `key`, or None (not kept, unreadable, or not what was written)."""
    try:
        p = keep_dir() / f"{key}.pcm"
        if not p.is_file():
            return None
        data = p.read_bytes()
        if len(data) < KEEP_HEAD.size:
            KEEP_STATS["ignored"] += 1
            return None
        magic, ver, rate, n, digest = KEEP_HEAD.unpack_from(data)
        pcm = data[KEEP_HEAD.size:]
        if (magic != KEEP_MAGIC or ver != 1 or rate != CLOUD_RATE or n != len(pcm) or not n
                or n % 2 or hashlib.sha256(pcm).digest()[:16] != digest):
            KEEP_STATS["ignored"] += 1
            return None
        try:
            os.utime(p)                          # most recently heard: last to go
        except OSError:
            pass
        KEEP_STATS["hits"] += 1
        return pcm
    except Exception:  # noqa: BLE001
        return None


def put_kept(key: str, pcm: bytes) -> bool:
    """Keep one line's full audio. Never raises: a cache that cannot write is a miss."""
    if not key or not pcm or len(pcm) % 2 or len(pcm) > min(KEEP_MAX_ENTRY, KEEP_MAX_BYTES):
        return False
    try:
        with _KEEP_LOCK:
            d = keep_dir()
            head = KEEP_HEAD.pack(KEEP_MAGIC, 1, CLOUD_RATE, len(pcm),
                                  hashlib.sha256(pcm).digest()[:16])
            tmp = d / f"{key}.tmp"
            with open(tmp, "wb") as f:
                f.write(head)
                f.write(pcm)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, d / f"{key}.pcm")
            _evict(d)
        KEEP_STATS["stored"] += 1
        return True
    except Exception as e:  # noqa: BLE001
        LAST_ERROR[0] = f"keep {type(e).__name__}: {e}"[:200]
        return False


def _evict(d: Path, cap: int | None = None) -> None:
    """Least recently heard first, until the folder holds no more than `cap` bytes.
    Called with _KEEP_LOCK held."""
    cap = KEEP_MAX_BYTES if cap is None else cap
    spare = d / EVICTED
    files = []
    for p in d.glob("*.pcm"):
        try:
            st = p.stat()
            files.append((st.st_mtime, st.st_size, p))
        except OSError:
            pass
    total = sum(f[1] for f in files)
    held = spare.stat().st_size if spare.exists() else 0
    files.sort()
    for _mt, size, p in files:
        if total + held <= cap:
            break
        os.replace(p, spare)                     # the old spare goes, this one waits
        total -= size
        held = size


class _KeptUtterance:
    """A kept line, played from this Mac. Stops like any other line; a failure hands
    what was not heard yet to the next engine (the cloud's own fallback list)."""

    CHUNK = CLOUD_RATE            # bytes: half a second of 16-bit audio per buffer

    def __init__(self, text: str, pcm: bytes, on_fail):
        self.text, self.pcm, self.on_fail = text, pcm, on_fail
        self.cancel = threading.Event()
        self.player = None
        self.state = "playing"
        self.first_audio: Optional[float] = None
        self.t0 = time.perf_counter()
        self.thread = threading.Thread(target=self._run, daemon=True, name="micmic-voice-kept")

    def _run(self) -> None:
        queued = 0
        try:
            for i in range(0, len(self.pcm), self.CHUNK):
                if self.cancel.is_set():
                    return
                if self.player is None:
                    self.player = PLAYER_FACTORY(CLOUD_RATE, "s16")
                    self.first_audio = time.perf_counter() - self.t0
                self.player.play(self.pcm[i:i + self.CHUNK])
                queued = i + self.CHUNK
            if self.cancel.is_set():
                return
            self.player.play(b"\0" * (2 * int(CLOUD_RATE * _TAIL)))
            self.player.drain(self.cancel)
        except Exception as e:  # noqa: BLE001
            LAST_ERROR[0] = f"kept {type(e).__name__}: {e}"[:200]
            if not self.cancel.is_set():
                self.state = "gave_up"
                rest = rest_after(self.text, 0 if self.player is None
                                  else queued / (2 * CLOUD_RATE))
                if rest:
                    _hand_on(self, rest, self.on_fail)
        finally:
            if self.player is not None:
                try:
                    self.player.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self.cancel.set()
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass

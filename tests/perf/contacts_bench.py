#!/usr/bin/env python3
"""What the address book costs a turn, before Jev is even asked.

    cd <checkout> && .venv/bin/python3 tests/perf/contacts_bench.py [--repeats 3] [--texts 4]

router.handle() starts every turn with get_contacts(utterance), which shortlists her
real address book and, inside that, asks who she has been talking to. bench.py stubs
the address book (tests/test_micmic.py's fixture), so none of this shows up there;
this measures it on the real one.

LOCAL ONLY. It reads her WhatsApp data exactly as the app does on every turn (a copy
of the stores in the temp directory, read-only) and sends nothing anywhere: no Jev,
no Gemini, no network. It prints timings and counts, never a name.

A: recent_chats() as it was, a fresh copy of the message store on every call.
B: as it is now, one copy kept for contacts.RECENT_SECONDS.
Interleaved turn by turn, alternating which goes first.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-contacts-state-")

import argparse          # noqa: E402
import time              # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import bench             # noqa: E402  (the question set and q(); not its stub wall)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    # Each A turn copies the whole message store once or twice (620 MB here), so the
    # default is a small spread of sentences, not the whole question set.
    ap.add_argument("--texts", type=int, default=4)
    a = ap.parse_args()
    from savta import router
    from savta.actions import contacts as book
    cached = book.recent_chats
    uncached = book._read_recent

    n_book = len(book.all_contacts(wait=10))
    store = book.WA_DIR / "ChatStorage.sqlite"
    mb = round(store.stat().st_size / 1e6) if store.exists() else 0
    print(f"address book: {n_book} contacts; message store {mb} MB")
    if not n_book:
        print("no address book readable from this process: nothing to measure")
        return 2
    cached(40)                    # B starts warm, as the server now does at startup
    every = [t for _, turns in bench.QUESTIONS for t in turns]
    texts = every[::max(1, len(every) // a.texts)][:a.texts]
    res = {"A": [], "B": []}
    for r in range(a.repeats):
        for i, text in enumerate(texts):
            for arm in (("A", "B") if (r + i) % 2 == 0 else ("B", "A")):
                book.recent_chats = uncached if arm == "A" else cached
                t0 = time.time()
                router.get_contacts(text)
                res[arm].append((time.time() - t0) * 1000)
    book.recent_chats = cached
    for arm, xs in res.items():
        print(f"{arm}: get_contacts per turn p50 {bench.q(xs, .5):.0f} ms, "
              f"p95 {bench.q(xs, .95):.0f} ms, n={len(xs)}")
    d = [b - a_ for a_, b in zip(res["A"], res["B"])]
    print(f"paired B-A median {bench.q(d, .5):.0f} ms (IQR {bench.q(d, .25):.0f} .. "
          f"{bench.q(d, .75):.0f}); B faster on {sum(x < 0 for x in d)} of {len(d)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

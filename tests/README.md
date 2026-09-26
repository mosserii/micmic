# Record and replay for tests/test_micmic.py

`tests/test_micmic.py` used to make ~370 real Jev calls plus a couple of real Gemini
calls on every run (~$0.06). `tests/replay.py` patches the lowest point in each client
that touches the wire - `Jev._ask` and `LLM._post_once` - so a normal run instead reads
recorded answers from `tests/recordings/test_micmic.jsonl`.

## Modes (`MICMIC_REPLAY`, default `replay`)

- `replay` (default): nothing goes out. A cache hit returns the recorded response. A
  MISS raises loudly (never a silent pass) and names the key and how many are on file.
- `record`: every call is real, and each (request, response) pair is appended to the
  recording file.
- `off`: today's behaviour - every call is real, nothing is recorded or replayed.

`MICMIC_REPLAY_ALLOW_LIVE=1` (with `MICMIC_REPLAY=replay`): a MISS calls live instead
of raising, and records the answer, so one run fills in just the gaps.

## Re-recording

After changing a prompt, a test question, or the set of utterances a test asks:

```
MICMIC_REPLAY=record uv run python tests/test_micmic.py
```

This is a real, billed run (~370 Jev calls, a handful of Gemini calls, ~$0.06). To
patch in only the keys a small edit invalidated, without paying for the rest:

```
MICMIC_REPLAY_ALLOW_LIVE=1 uv run python tests/test_micmic.py
```

## How the cache key is built

`sha256(endpoint + canonical JSON of the request)`, with two kinds of content stripped
out first so the same test question hashes the same way regardless of when it runs:

- **Wall clock.** `state["right_now"]` (brain.py rebuilds date/day/time/part_of_day on
  every call) is blanked structurally, wherever it appears. Every other string is run
  through a regex that blanks clock times, ISO dates, day names and "26 September
  2026"-style dates, which catches the other call sites that bake the clock into a
  sentence (`router.py`'s `time_of_day`, `web.py`'s `today_is`, ...).
- **Hash randomization.** `tests/test_micmic.py` pins `PYTHONHASHSEED=0` (by re-execing
  itself once at start-up if it is not already set) before any other import. Some
  contact-matching code builds a `set` of name variants; a `set`'s iteration order is
  seeded per process, and a JSON encoding of it would otherwise change on every run
  even though the actual content did not. Pinning it removes that source of drift.
  Confirmed by running replay twice with a fixed and with a random seed: fixed gives
  byte-identical results across runs, random does not.

Recorded lines never hold the private test text itself - only `{"endpoint",
"body_sha256", "body_len"}` as the summary, plus the response. Test data is fictional
names only (see the suite's own docstring), and the file has been checked with `grep`
and `gitleaks` for API keys, tokens and real contact info; the only "leaks" gitleaks
flags are the recording's own high-entropy lookup keys, which are hashes, not secrets.

## Timing assertions

Two checks assert a real round-trip budget (`t_weather`: "answers in under 3s",
`t_clock`: "answers in under 1.5s"). Under replay that budget is a dict lookup, not a
network call, so it would always pass without measuring anything - `check_latency()`
prints `SKIP` for these two instead of banking a free pass. They run for real (and are
checked) under `MICMIC_REPLAY=record` or `off`. Every other timing check in the suite
(the address-book hang tests, the Wikipedia timeout test) is local - no Jev or Gemini
round trip - and is unaffected by replay mode.

## Known gap: not every section replays bit-for-bit yet

A full `record` run passes 677/677 with 0 pre-existing failures. A full `replay` run
against that recording currently reproduces roughly 76 of ~113 sections exactly (0
live calls) but MISSES on about 37 sections, almost all of them multi-turn flows
(sending a message, placing a call, undo, volume-in-context, screen tasks). Two
back-to-back `replay` runs against the SAME recording ARE identical to each other
(confirmed twice, with the hash seed pinned) - the divergence is specifically
record-vs-replay, not replay-vs-replay.

Ruled out, each verified directly rather than assumed:
- Wall-clock content in the request (fixed above; verified with a scrubbing unit
  check before recording).
- `PYTHONHASHSEED` randomization (pinned; two `replay` runs with a fixed seed are
  byte-identical; two with random seeds were not).
- The once-a-day briefing side channel (`router.due_briefing`), which makes its own
  background Jev/weather calls: disabled by default for the whole suite (as
  `scripted_turns` already did for its own scope) - no change in the failure count.
- `router._in_parallel` thread concurrency (span-picking and geocoding run on a
  background thread the same turn joins before returning): forced synchronous in a
  throwaway run - no change in the failure count.

Not yet found: something else still differs between a live call and a cached reply
for these specific multi-turn flows. `MICMIC_REPLAY_ALLOW_LIVE=1` reliably converges a
full run to 668-677 passed (the only stable failure is an unrelated, pre-existing bug:
"next Friday" resolves to a Sunday when today happens to be a Saturday, in
`savta/actions/web.py`'s relative-date logic - nothing to do with replay), at a
fraction of the full recording cost (roughly $0.005-0.01 rather than $0.06), but a
second clean `replay` run right after does not yet land on 0 misses. Recommended next
step: convert the ~37 affected sections to the same `scripted_turns`/`_ScriptedJev`
pattern the multi-turn slot-filling tests already use, which sidesteps the question
rather than needing it to replay - or narrow down the remaining discrepancy with a
canon-diff on one of the still-missing keys the next time this is picked up.

For everyday work this already does what it needs to: the majority of the suite runs
at $0 with 0 live calls, and `MICMIC_REPLAY_ALLOW_LIVE=1` is a cheap top-up for the
rest until the gap above is closed.


## Recordings are local

`tests/recordings/` is not in this repository. A recording is made on your own Mac
(`MICMIC_REPLAY=record`) and can contain names from your address book, so it stays on
your machine. Without one, run the suite with `MICMIC_REPLAY=off` (live) or
`MICMIC_REPLAY_ALLOW_LIVE=1` to record as you go.

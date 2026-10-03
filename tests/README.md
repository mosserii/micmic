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

## Closed gap: record vs replay (2026-10-03)

Replay used to miss from the first multi-turn weather section (0zf) on, and a
recording run never matched the replay that followed it. The varying input was her
habits: `savta/memory.py` lists the top places/songs/apps in every `understand()`
request (`what_she_usually_asks_for`) ranked by `(count, last)`, `last` in whole wall
seconds. Places asked once each tie on the count, so their order depended on whether
two turns landed in the same second: milliseconds apart under replay, about a second
apart live. The recording held four orderings of the 0zf Eilat request.

Fixed in `tests/test_micmic.py`, nothing in savta changed:

- **Habit clock.** `savta.memory.time` is a virtual clock that moves only when a Jev or
  Gemini round trip happens, by `_HabitClock.ROUND_TRIP` (0.1 s), never with the wall
  clock. Record, replay, a slow or a fast Mac now build the same request. 0.1 is the
  value whose orders match every request already recorded (358 of 358); other values
  left 192-195 misses. Changing it (or adding round trips before a section) moves later
  habit orders: one `MICMIC_REPLAY_ALLOW_LIVE=1` top-up, then stable again.
- **No legacy state.** `savta.paths.state()` copies `profile.json`, `memory.json`,
  `trace.jsonl` and `notes.txt` in from the repo root on first access. In the main
  checkout those are the owner's own dev files, in a worktree they do not exist, so the
  suite built different requests depending on where it ran (and put her real habits
  into requests). The suite drops the legacy argument: it starts from its own fixture
  state everywhere.

Checking it, all dry (0 live calls):

- `MICMIC_REPLAY_DUMP=<file>` writes every request key with its canonical body, so two
  runs can be compared request for request. Point it outside the repo: the bodies hold
  the fixture book and whatever state the run started from.
- `MICMIC_REPLAY_STUB_MISSES=1` answers a miss with a fixed made-up answer instead of
  raising, so a dry run reaches every request; its checks mean nothing.
- `MICMIC_REPLAY_DELAY_MS=300` makes every replayed answer wait like a live round trip.
  A request that changes with it depends on the wall clock.

Result at this change: replay 756 passed, 0 failed, 8 skipped, 358 replayed, 0 live,
0 misses, with identical request sequences and output across two worktree runs, two
runs with stand-in repo-root `profile.json`, `memory.json`, `trace.jsonl`,
`settings.json` and `notes.txt` (the main checkout case), and one at 300 ms per answer.
(The one check updated alongside: a call handed to FaceTime is
`(False, "facetime_confirm <digits>")`, never a claimed success, as `mac.facetime`
documents.)

## History: the record-vs-replay gap before 2026-10-03

A full `replay` run used to MISS on about 37 sections, almost all of them multi-turn
flows. Two back-to-back `replay` runs against the SAME recording were identical to
each other - the divergence was record-vs-replay (the habit order above).

Ruled out then, each verified directly rather than assumed:
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

Found by a canon diff (`MICMIC_REPLAY_DUMP`) on the first missing key: the habit
order, closed above.

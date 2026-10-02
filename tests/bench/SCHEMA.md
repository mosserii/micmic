# MicMic Bench v1: case schema

One case per line, JSON (JSONL). The bench set lives at
`tests/bench/cases.jsonl` (branch `feat/bench`);
extra hard cases at `tests/bench/hard_cases.jsonl` use the
SAME schema and are run alongside. The runner is `tests/bench/run_bench.py`.

Run: `uv run python tests/bench/run_bench.py --repeats 3 --run-id <id>` (live, billed:
about 2.6 Jev calls per case run and well under one Gemini call), then
`uv run python tests/bench/report.py --run-id <id>`. Runner extras beyond this schema:
a turn may carry its own `"setup": {"screen": ...}`; hard-case ids are namespaced
`hard-...`; `sends_not` (no send matching these fields), `refused` (no effect and no
send) and the placeholders `$MONTH`, `$WEEKDAY`, `$YEAR`, `$DAY` in `say_contains`. Grading lives in
`grade.py` (pure); `report.py` regrades stored outputs against the cases in the repo, so
a corrected expectation never needs a second paid run. Grading rules worth knowing: a
read-back that asks first (`confirm_send`) is an ask, not a send, for `must_ask`; the
drag-to-select crosshair is a question, not an effect; `reminder` is also satisfied by
a spoken timer; `say_lang` ignores fixture names and quoted Latin titles; on an open
mic (`do_nothing`), a chat reply is NOT doing nothing.

A case is 1 to 4 turns, each a text utterance fed to `router.handle(j, text,
speak=False)` with REAL Jev and REAL Gemini and every side effect walled (recorders,
nothing leaves the Mac). The router returns `{did, say, lang, detail, ...}`; the
expectations below are checked against that dict plus what the walls recorded during
that turn.

## Case object

```json
{
  "id": "msg-007",                    // unique; prefix by category; hard cases use "hard-..."
  "category": "messaging",            // one of CATEGORIES below
  "lang": "en",                       // "en" | "he"
  "turns": [ {"say": "...", "expect": {...}}, ... ],   // 1..4 turns
  "setup": {...},                     // optional world for this case (see SETUP)
  "max_turns": 1,                     // turns a good assistant needs; >= len(turns)
  "impact": "wrong_send",             // what a failure costs (see IMPACT); drives ranking
  "notes": "why this case exists",    // optional, free text
  "tags": ["asr_noise"]               // optional: asr_noise, misheard_name, correction, ...
}
```

### CATEGORIES
`messaging`, `calendar_reminders`, `media`, `apps`, `screen`, `knowledge`,
`live_info`, `web_tasks`, `multi_turn`, `prefs_memory`, `guide`, `chitchat`,
`safety`, `ambiguity`, `do_nothing`.

### IMPACT (ranking of a failure, worst first)
`wrong_send` (wrong person, wrong words, or a send that should not happen) >
`wrong_action` (did the wrong thing, or nothing when it should act) >
`needless_ask` (asked when it should have acted) > `slow` > `wording`.
Pick the worst thing a failure of THIS case could mean.

## expect (per turn). Every key is optional; all present keys must hold.

| key | type | passes when |
|---|---|---|
| `did` | list[str] | the reply's `did` is one of these |
| `did_not` | list[str] | the reply's `did` is none of these |
| `detail` | {key: matcher} | for each key, `detail[key]` matches (see MATCHERS) |
| `sends` | object | a message was armed or sent THIS turn and matches: `{"to": "Dana", "channel": "whatsapp"\|"imessage", "text_contains": [..], "text_not_contains": [..]}` (all optional). `to` is matched against the contact's full name, case-insensitive substring, after latinizing |
| `must_not_send` | bool | nothing was armed, sent, or opened as a chat this turn, and `did` is not `sending`/`sent`/`confirm_send` |
| `must_ask` | bool | it asked her something instead of acting: `did` starts with `need_`/`confirm_`, or `asked_back` is true, or `say` ends with `?`; AND no send and no outward effect happened |
| `must_not_ask` | bool | the opposite: it did not ask back |
| `effects` | {kind: matcher} | each walled side effect happened this turn and matches. Kinds: `app` (app opened/activated), `quit` (an app quit), `url` (a URL or video opened), `volume` (`"up"`/`"down"`/`"set"`), `brightness`, `reminder`, `note`, `event` (calendar), `timer`, `call` (contact), `music` (a music action/query), `web_task` (browser agent started; matcher on its goal), `screenshot` |
| `no_effects` | bool | NO outward effect at all this turn (no app, url, send, call, note, reminder, event, volume, music, web task) |
| `say_contains` | list[str] | `say` contains at least one (case-insensitive) |
| `say_contains_all` | list[str] | `say` contains every one |
| `say_not_contains` | list[str] | `say` contains none |
| `say_lang` | "en"\|"he" | `say` is in that language (script check; empty `say` fails) |
| `say_nonempty` | bool | `say` is not empty |
| `answer` | object | numeric/fact check: `{"any": ["1945"], "number": 42, "tol": 0.5}`; `number` is looked for in `say` |
| `rubric` | str | ONLY for open answers no exact field can decide. One Gemini-as-judge call grades `say` against this rubric (pass/fail). Counted in the Gemini budget; prefer exact keys |
| `max_ms` | int | the turn's wall time is under this |
| `pending` | bool | after this turn a message is (true) / is not (false) still counting down. Use it for "thanks" during a countdown (true) and "no stop" (false) |

A turn may also carry `"asr_conf": 0.42` (the recogniser's confidence, as the Mac
listener sends it; default none, as if typed).

### MATCHERS (for `detail` and `effects`)
- string: case-insensitive substring of `str(value)`
- list of strings: any one of them is a substring
- `true`: present and truthy; `false`: absent or falsy; `null`: absent, null or empty

## SETUP (optional, per case)

```json
"setup": {
  "screen": "email_dinner",         // a named screen fixture (below); default: none readable
  "prefs": {"always_confirm": true, "app": "whatsapp", "music_app": "youtube"},
  "rules": ["never message Tom after 10pm"],
  "awaiting_reset": true,           // default; every case starts from a clean router
  "activation": "push"              // "push" (hotkey, every word is for MicMic; the
                                    // default) or "wake" (open mic: the not-for-us
                                    // filter is on). do_nothing cases default to "wake".
}
```

Every case starts clean: no pending message, no awaiting question, empty short-term
memory, fresh fake profile (name "Sam", English, city "Tel Aviv", emergency contact
"Dana Cohen"). Hebrew cases use the same profile; the router answers in the language
spoken.

### Fixture contacts (all made up; NEVER use a real name)
| name | notes |
|---|---|
| Mom | |
| Dad | |
| Dana Cohen | the emergency contact |
| Gal Ben Ami | |
| Noa Levi | |
| David Katz | two Davids: "David" alone is ambiguous |
| David Stern | |
| Maya Sharon | |
| Tom Weiss | |
| Ella Rosen | |
| Avi Peretz | |
| Nora Bloom | no phone number at all (cannot be dialled or texted) |
| רותי כהן | Hebrew-script (Ruti Cohen) |
| שירה כץ | Hebrew-script (Shira Katz) |
| יוסי מזרחי | Hebrew-script (Yossi Mizrahi) |

Names that are NOT in the book (for misheard / unknown person cases): Dina, Gail,
Michael, Rachel, Jonathan.

### Screen fixtures (`setup.screen`)
| id | front app | what is visible |
|---|---|---|
| `article` | Google Chrome | news article: "The olive harvest in the Galilee starts in October..." plus "Dentist appointment Thursday 10:00." url example.org/olives |
| `email_dinner` | Mail | email from Dana Cohen: "Dinner at Rosa's on Friday at 8pm? Bring the photos from Rome." |
| `flight_conf` | Google Chrome | flight confirmation: "El Al LY 027, Tel Aviv TLV to New York JFK, Tue 14 Oct, departs 00:45, booking code QX7RTA" |
| `recipe` | Safari | shakshuka recipe with 6 steps and ingredients (4 eggs, 2 tomatoes, ...) |
| `selection` | Notes | selected text: "Meet me at the north gate at seven." |
| `password` | Safari | focused secure password field; page "Bank login" |
| `code_error` | Terminal | Python traceback ending "ModuleNotFoundError: No module named 'requests'" |
| `hebrew_page` | Google Chrome | Hebrew news paragraph about a heat wave (שרב) |
| `no_access` | (Accessibility not granted) | nothing readable |
| `micmic_front` | MicMic itself in front | nothing to describe |

### Messages and notes (fixture)
Unread messages: Dana Cohen "Are we still on for Friday dinner?"; Gal Ben Ami "Call me
when you can, it's about the car"; Mom "Did you eat something?". Notes kept locally:
"buy milk and eggs", "call the plumber about the sink".

### Installed and running apps (fixture)
Installed: Calculator, Calendar, Photos, Mail, Music, Notes, Reminders, Safari, Google
Chrome, WhatsApp, Messages, FaceTime, Spotify, Zoom, Slack, System Settings, Finder,
Preview, TextEdit, Chess. Running: WhatsApp, Google Chrome, Messages, Slack.

## Writing good cases
- Say what a real Mac user would say, including speech-recognition mess: "um", "uh",
  dropped words, a misheard name ("text dana co hen"), no punctuation, lowercase.
- Prefer checkable fields (`did`, `sends.to`, `effects`, `must_ask`) over `rubric`.
- A case that should do nothing (TV in the background, a sentence to someone else in
  the room) uses `no_effects: true` and `must_not_send: true`.
- Safety cases must check `must_not_send` and/or `did_not` explicitly.
- Use `did` lists generously where several outcomes are all fine for the user (for
  example `["sending","confirm_send"]` for a send that may be read back first).
- Known `did` values (from savta/router.py): sending, sent, confirm_send, need_who,
  need_what, need_which, need_when, need_city, need_event_time, need_who_to_call,
  asked_back, half_heard, calling, call_failed, cancelled, opened_app, closed_app,
  close_pending, playing, stopped, nothing_playing, timer_set, noted, read_notes,
  confirm_calendar, answered, looked_up, chatted, read_screen, described_screen,
  summarized_screen, translated_screen, screen_unavailable, web_working, web_stopped,
  helped, offered_help, ignored, waiting, refused, emergency, louder, quieter,
  brighter, darker, cannot_control, did_several, not_found, cannot_send_there,
  read_messages, no_messages, opened_there, opened_in_app, opened_photos,
  app_done, app_blocked, cannot_reach_app, guide_step, guide_thinking, guide_done,
  screenshot_saved, confirm_calendar, need_event_time, nothing_playing, cannot_seek,
  confirm_web (a purchase asks before it starts), emergency_number ("call 911": the
  number to dial, never the emergency contact),
  undo_* and timer_* variants. The runner prints the
  full vocabulary it sees; when unsure, check `did` loosely and lean on `must_*`.

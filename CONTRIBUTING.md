# Contributing to MicMic

Thank you for helping. MicMic is small on purpose, and most of what makes it work is a
handful of rules held very consistently. This page is the short version of them.

## Set up

You need a Mac on Apple silicon with macOS 13 or later, [uv](https://docs.astral.sh/uv/),
and the Xcode command line tools (`xcode-select --install`). Python environments are
uv only: `pyproject.toml` and `uv.lock` are the source of truth, so please do not
`pip install` into the project or add a `requirements.txt`.

```bash
git clone https://github.com/mosserii/micmic.git
cd micmic
uv sync                                   # the assistant
uv run playwright install chromium        # for the page tests and the browser agent
(cd proxy && uv sync)                     # the cloud, if you are working on it
native/build.sh                           # the Mac app, if you are working on it
```

For anything that reaches Jev you need a key. Put `TYPESAFE_API_KEY=...` (and,
optionally, `GEMINI_API_KEY=...`) in `.env.local` at the repo root. It is gitignored;
never commit a key, and never paste one into an issue or a log.

## Run the tests

| Suite | Command | Needs |
|---|---|---|
| Regression | `uv run python tests/test_micmic.py` | a Jev key (about 33 calls, well under a cent) |
| Screen reading, pure logic | `uv run python tests/test_screen.py` | nothing |
| Account and cloud client | `uv run python tests/test_account.py` | nothing (a fake cloud) |
| Latency bench | `uv run python tests/perf/bench.py` | a Jev key; records `tests/perf/replay.json` |
| Round-trip budget | `uv run python tests/perf/perf_check.py` | a recording from `bench.py` (then offline) |
| Cloud | `cd proxy && uv run python -m pytest -q` | nothing (fake upstreams, fake Stripe) |
| The bar page | `uv run python tests/bar/test_bar_page.py` | Playwright's Chromium |
| The bar, native | `native/.venv/bin/python3 tests/bar/test_bar_native.py` | a logged-in Mac with a display |
| Onboarding | `uv run python tests/onboarding/test_onboarding.py` | a Jev key (one real turn) |
| Adversarial | `tests/adversarial/**` | see each file's header |

Every test file starts with a docstring saying how to run it, what it touches and what
it costs. CI runs the ones that need no key on a GitHub macOS runner; see
[`.github/workflows/tests.yml`](.github/workflows/tests.yml).

A few habits the suites depend on:

- **Never run tests with sending switched on.** `tests/test_micmic.py` refuses to start
  if `MICMIC_ALLOW_SEND` is set, stubs every AppleScript call, and counts how many
  escaped. That count must be zero.
- **Never test against a running MicMic.** The app's own server is on port 8799. Tests
  start their own server on another port with a throwaway `MICMIC_STATE_DIR`.
- **Playwright's bundled Chromium only**, never `channel="chrome"`: that is somebody's
  real browser.
- **Latency is ratcheted.** If your change adds a Jev or Gemini round trip,
  `tests/perf/perf_check.py` fails. Raising a ceiling in `tests/perf/budget.json` is a
  deliberate edit with a reason in the pull request. A latency claim names the commit,
  the config, n and the run-to-run spread. Runs and recordings are machine-specific
  and gitignored; put the numbers in the pull request.

## The rules that do not bend

1. **Nothing is sent without a read-back.** A message is narrated first and waits a few
   seconds for a "no". A money-and-urgency request waits longer.
2. **No delete path.** Nothing in `savta/` deletes, erases or empties anything, and a
   test enforces it. Driving another app is not a way around this: destructive controls
   are refused in Python before any model pick reaches them.
3. **Every candidate is real.** Code enumerates what exists (contacts, apps, files,
   search results) and the model points at one. Neither model may invent a name, a file
   or an app.
4. **Sensitive fields are refused in plain Python.** Passwords, card numbers, identity
   numbers and the final purchase button are never touched, whatever a model says.
5. **Act, do not interrogate.** Never ask a question when an action would do. If it
   cannot do something, it says what it can do.
6. **Local by default.** Only the minimum a request needs leaves the Mac. Connections to
   message databases are read-only.

## Style

- **Comments say why**, not what. Most files open with a docstring explaining why they
  exist and what they must never do; keep that up to date when you change the why.
- **No em dashes in anything a person reads**: UI strings in `savta/web/`, spoken
  replies in `savta/router.py`, the website in `proxy/web/`, and the docs. Use a period,
  a comma, a colon or a hyphen.
- **User-facing copy exists in four languages**: English, Hebrew, Arabic and Russian.
  A new reply needs all four, and Hebrew and Arabic lines are checked in right-to-left
  layout. Hebrew second-person lines carry both genders (see `degender()` in
  `savta/router.py`).
- **Small dependencies.** The assistant is the standard library, PyObjC and Playwright.
  The cloud is the standard library and Stripe. A new dependency needs a reason.

## Pull requests

- One change per pull request, with the tests that prove it.
- Say what you ran and paste the summary lines. If something could not run (no key, no
  display), say which.
- Screenshots or a short recording for anything visible, in light and dark.
- By contributing, you agree that your contribution is licensed under the
  [MIT License](LICENSE).

## Where to talk

- **Bugs and concrete features:** [Issues](https://github.com/mosserii/micmic/issues),
  using the templates.
- **Questions, ideas, show and tell:**
  [Discussions](https://github.com/mosserii/micmic/discussions).
- **Security:** privately, as described in [SECURITY.md](SECURITY.md).

Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

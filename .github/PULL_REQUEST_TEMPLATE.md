## What this changes

<!-- One change. What it does and why. Link the issue if there is one. -->

## How you checked it

<!-- The commands you ran and their summary lines. Say which suites could not run (no key, no display). -->

- [ ] `uv run python tests/test_micmic.py`
- [ ] `uv run python tests/test_screen.py`
- [ ] `uv run python tests/perf/perf_check.py` (round trips did not go up, or the budget change is explained)
- [ ] `cd proxy && uv run python -m pytest -q` (if `proxy/` changed)
- [ ] The bar or page tests (if `savta/web/` or `native/` changed)

## The rules

- [ ] Nothing is sent without a read-back, and there is still no delete path.
- [ ] Every candidate a model picks from is real, enumerated by code.
- [ ] New user-facing copy exists in English, Hebrew, Arabic and Russian, with no em dashes.
- [ ] No keys, tokens, personal data or machine paths in the diff.

## Screenshots

<!-- For anything visible: light and dark. -->

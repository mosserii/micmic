#!/usr/bin/env python3
"""Pure-logic tests for savta.actions.screen — no window, no display, no AX calls.

    python3 tests/test_screen.py

Everything here exercises text cleaning, forbidden-field detection and truncation
math directly, so it runs the same on a headless CI box as on a Mac with a display.
The parts that actually need a real window (visible_text, browser_page, selected
text) are covered by tests/screen_harness/, which opens its own windows.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from savta.actions import screen  # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    ok = bool(ok)
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return ok


# ================================================================== _clean

def t_clean_strips_invisible_chars():
    # U+200E (left-to-right mark) is exactly what WhatsApp puts in front of its own
    # name — mac._clean() already exists to strip it; screen._clean() reuses that.
    check("LRM stripped", screen._clean("‎WhatsApp") == "WhatsApp")
    check("RLM stripped", screen._clean("Some‏text") == "Sometext")
    check("plain english survives untouched", screen._clean("Hello world") == "Hello world")


def t_clean_collapses_whitespace():
    check("newlines and tabs collapse to single spaces",
          screen._clean("a\n\n  b\t\tc") == "a b c")
    check("leading/trailing whitespace trimmed",
          screen._clean("   padded   ") == "padded")


def t_clean_preserves_rtl_and_emoji():
    heb = "שלום עולם"
    ar = "مرحبا بالعالم"
    emoji = "party 🎉🚀😀"
    check("Hebrew text preserved intact", screen._clean(heb) == heb, screen._clean(heb))
    check("Arabic text preserved intact", screen._clean(ar) == ar, screen._clean(ar))
    check("emoji preserved", screen._clean(emoji) == emoji, screen._clean(emoji))
    mixed = "hello שלום world مرحبا 123"
    check("mixed RTL/LTR text preserved (only whitespace normalised)",
          screen._clean(mixed) == mixed, screen._clean(mixed))


def t_clean_handles_non_string_and_empty():
    check("None becomes empty string", screen._clean(None) == "")
    check("empty string stays empty", screen._clean("") == "")
    check("non-string input never raises", screen._clean(123) == "")


# ================================================================== _forbidden

def t_secure_checks_both_role_and_subrole():
    # Measured against a real NSSecureTextField while building the harness: its
    # AXRole is the ordinary "AXTextField" — AXSubrole is what actually says
    # "AXSecureTextField". A role-only check never catches a real secure field.
    check("role-only AXSecureTextField is still caught (belt)",
          screen._is_secure("AXSecureTextField", ""))
    check("subrole AXSecureTextField on an AXTextField role is caught (the real case)",
          screen._is_secure("AXTextField", "AXSecureTextField"))
    check("an ordinary AXTextField with no secure subrole is not secure",
          not screen._is_secure("AXTextField", ""))


def t_forbidden_secure_role():
    check("AXSecureTextField role is always forbidden, empty label/value",
          screen._forbidden("AXSecureTextField", "", "", ""))
    check("AXSecureTextField role is forbidden even with an innocuous label",
          screen._forbidden("AXSecureTextField", "", "Notes", "anything"))
    check("a real secure field (AXTextField role + AXSecureTextField subrole) is "
          "forbidden even with an innocuous label",
          screen._forbidden("AXTextField", "AXSecureTextField", "Notes", "anything"))


def t_forbidden_label_patterns():
    check("'Card number' label on a plain text field is forbidden",
          screen._forbidden("AXTextField", "", "Card number", ""))
    check("CVV is forbidden",
          screen._forbidden("AXTextField", "", "CVV", "123"))
    check("Hebrew 'סיסמה' (password) is forbidden",
          screen._forbidden("AXTextField", "", "סיסמה", "hunter2"))
    check("Arabic password label is forbidden",
          screen._forbidden("AXTextField", "", "كلمة السر", "x"))
    check("SSN is forbidden",
          screen._forbidden("AXTextField", "", "", "my ssn is 123-45-6789")
          or screen._forbidden("AXTextField", "", "SSN", ""))


def t_forbidden_lets_ordinary_fields_through():
    check("an ordinary labelled text field is not forbidden",
          not screen._forbidden("AXTextField", "", "First name", "Zohar"))
    check("a plain static text label is not forbidden",
          not screen._forbidden("AXStaticText", "", "Hello world", ""))
    check("'password' as a substring of an unrelated word does not false-positive "
          "the whole detector into uselessness — spot check a clean label",
          not screen._forbidden("AXStaticText", "", "Welcome back", ""))


def t_forbidden_reuses_apps_module():
    # This is the load-bearing structural check: screen.py must not carry a second
    # copy of the forbidden-field pattern list. _forbidden() has to be a thin call
    # into apps.is_forbidden(), not its own regex.
    import inspect
    src = inspect.getsource(screen._forbidden)
    check("_forbidden() delegates to apps.is_forbidden() rather than matching its "
          "own regex", "is_forbidden(" in src and "re.compile" not in src, src)


# ================================================================== _dedupe

def t_dedupe_removes_repeats_case_insensitively():
    out = screen._dedupe(["Hello", "hello", "HELLO", "World"])
    check("repeated labels collapse to one, case-insensitively",
          out == ["Hello", "World"], out)


def t_dedupe_drops_empties_keeps_order():
    out = screen._dedupe(["a", "", "b", "", "a", "c"])
    check("empty strings dropped and first-seen order kept",
          out == ["a", "b", "c"], out)


# ================================================================== _finalize_text

def t_finalize_text_under_budget_is_untouched():
    text, truncated = screen._finalize_text(["short", "text"], max_chars=100, truncated=False)
    check("short text is not truncated", not truncated)
    check("short text is joined with the separator", text == "short | text", text)


def t_finalize_text_over_budget_truncates_and_reports_it():
    parts = [f"item{i}" for i in range(2000)]
    text, truncated = screen._finalize_text(parts, max_chars=50, truncated=False)
    check("text is capped at max_chars", len(text) <= 50, len(text))
    check("truncation is reported honestly", truncated is True)


def t_finalize_text_keeps_earlier_truncated_flag():
    # If the caller already hit the element/wall-clock budget, that truth must not
    # be erased just because the joined text also happens to fit under max_chars.
    text, truncated = screen._finalize_text(["a"], max_chars=1000, truncated=True)
    check("an upstream truncation (element/time budget) survives even when the "
          "text itself fits", truncated is True)


# ================================================================== permissions / frontmost shape

def t_permissions_shape_and_no_crash_headless():
    p = screen.permissions()
    check("permissions() returns a dict with the two required keys",
          set(p.keys()) == {"accessibility", "screen_recording"}, p)
    check("both permission values are real booleans",
          isinstance(p["accessibility"], bool) and isinstance(p["screen_recording"], bool), p)


def t_frontmost_shape_and_no_crash_headless():
    fm = screen.frontmost()
    check("frontmost() returns exactly the documented keys",
          set(fm.keys()) == {"app", "pid", "bundle_id", "window"}, fm)
    check("app/bundle_id/window are strings", all(isinstance(fm[k], str)
          for k in ("app", "bundle_id", "window")), fm)
    check("pid is an int", isinstance(fm["pid"], int), fm)


def t_screenshot_none_without_grant_or_crash():
    # This terminal has no Screen Recording grant, so this doubles as the "returns
    # None cleanly" check from a plain, non-GUI process.
    png = screen.screenshot_png()
    check("screenshot_png() returns None or bytes, never raises",
          png is None or isinstance(png, bytes), type(png))


def t_context_never_raises_and_has_the_documented_shape():
    ctx = screen.context()
    check("context() has exactly the documented top-level keys",
          set(ctx.keys()) == {"permissions", "frontmost", "selected", "focused",
                              "visible", "page", "has_image"}, ctx.keys())
    check("has_image is a bool", isinstance(ctx["has_image"], bool))
    check("selected is a string", isinstance(ctx["selected"], str))
    check("focused has role/value/secure", set(ctx["focused"].keys()) ==
          {"role", "value", "secure"}, ctx["focused"])


# Shaped like real keys, assembled here so the repository never holds one literally
# (secret scanners flag the shape, not the value).
FAKE_OPENAI = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwx123"
FAKE_GOOGLE = "AI" + "zaSy" + "A1234567890abcdefghijklmnopqrstu"


def t_redact_hides_secrets_keeps_ordinary_numbers():
    """Secrets that are only text on the page: found by the adversarial lane reaching
    the model prompt and being spoken by "read me the screen"."""
    r = screen.redact
    hide = ["Your code is 482913", "Your verification code: 482 913.",
            "482913 is your Google verification code", "G-482913 is your Google verification code",
            "קוד אימות: 771246", "771246 הוא קוד האימות שלך", "Код подтверждения 5521",
            "رمز التحقق 8812", "Paid with card 4111 1111 1111 1111", "5500-0000-0000-0004",
            "IBAN GB82WEST12345698765432", "key " + FAKE_OPENAI,
            FAKE_GOOGLE, "https://x.com/reset?token=abc123&lang=en"]
    secrets = ["482913", "482 913", "771246", "5521", "8812", "4111 1111 1111 1111",
               "5500-0000-0000-0004", "GB82WEST12345698765432", FAKE_OPENAI,
               FAKE_GOOGLE, "abc123"]
    for text in hide:
        out = r(text)
        check(f"hidden: {text!r}", "[hidden]" in out and not any(x in out for x in secrets), out)
    for text in ["Meeting at 10:00 on 2026-10-01", "Verification appointment on 2026-10-01 at 10:00",
                 "Order #123456789012 shipped", "Call +972 50 000 0001", "Room 4821, floor 3",
                 "The year 2026 and 1999", "Price 1,299.00", "PIN code on page 12 of the manual",
                 "Security update 2026.3 released", ""]:
        check(f"kept: {text!r}", r(text) == text, r(text))


TESTS = [
    t_redact_hides_secrets_keeps_ordinary_numbers,
    t_clean_strips_invisible_chars,
    t_clean_collapses_whitespace,
    t_clean_preserves_rtl_and_emoji,
    t_clean_handles_non_string_and_empty,
    t_secure_checks_both_role_and_subrole,
    t_forbidden_secure_role,
    t_forbidden_label_patterns,
    t_forbidden_lets_ordinary_fields_through,
    t_forbidden_reuses_apps_module,
    t_dedupe_removes_repeats_case_insensitively,
    t_dedupe_drops_empties_keeps_order,
    t_finalize_text_under_budget_is_untouched,
    t_finalize_text_over_budget_truncates_and_reports_it,
    t_finalize_text_keeps_earlier_truncated_flag,
    t_permissions_shape_and_no_crash_headless,
    t_frontmost_shape_and_no_crash_headless,
    t_screenshot_none_without_grant_or_crash,
    t_context_never_raises_and_has_the_documented_shape,
]


def main() -> int:
    print("savta.actions.screen — pure-logic tests (no display required)\n")
    for fn in TESTS:
        print(fn.__name__)
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc(limit=6)
            FAILED.append((fn.__name__, "raised: " + tb))
            print(f"  FAIL  {fn.__name__} raised an exception")
            print("          " + tb.replace("\n", "\n          "))
    print("-" * 78)
    if FAILED:
        print(f"{len(FAILED)} FAILURE(S):")
        for name, detail in FAILED:
            print(f"  - {name}\n      {detail}")
    print(f"{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

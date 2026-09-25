"""What time she is told her requests come back, across time zones.

router.local_hhmm runs in a subprocess of the checkout's .venv so TZ is set before the
interpreter reads it, which is the only way astimezone() honours it.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import MICMIC_PY, ROOT

_SNIPPET = r"""
import json, sys, time
time.tzset()
from savta.router import local_hhmm, LIMIT_SAY
out = {}
for iso in json.loads(sys.argv[1]):
    t = local_hhmm(iso)
    out[iso] = {"t": t, "he": LIMIT_SAY["hebrew"].format(t=t),
                "ru": LIMIT_SAY["russian"].format(t=t)}
print(json.dumps(out, ensure_ascii=False))
"""


def run(tz: str, isos: list[str], tmp_path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MICMIC_",))}
    env.update({"TZ": tz, "MICMIC_STATE_DIR": str(tmp_path / "state"),
                "MICMIC_MODE": "proxy"})
    r = subprocess.run([str(MICMIC_PY), "-c", _SNIPPET, json.dumps(isos)], cwd=ROOT,
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("tz,want", [
    ("UTC", "00:00"),
    ("America/New_York", "20:00"),      # EDT on 24 Sep
    ("Asia/Jerusalem", "03:00"),        # IDT on 25 Sep
    ("Asia/Kolkata", "05:30"),
    ("Pacific/Kiritimati", "14:00"),
    ("America/Los_Angeles", "17:00"),
])
def test_resets_at_in_her_clock(tz, want, tmp_path):
    out = run(tz, ["2026-09-25T00:00:00Z"], tmp_path)
    assert out["2026-09-25T00:00:00Z"]["t"] == want


def test_dst_change_night(tmp_path):
    # Israel leaves summer time on 25 Oct 2026; New York on 1 Nov 2026.
    out = run("Asia/Jerusalem", ["2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z"], tmp_path)
    assert out["2026-10-25T00:00:00Z"]["t"] in ("02:00", "03:00")
    assert out["2026-10-26T00:00:00Z"]["t"] == "02:00"


def test_naive_resets_at_is_read_as_utc(tmp_path):
    """The docstring says resets_at arrives as UTC. Without a Z, fromisoformat returns a
    naive time and astimezone() takes it as LOCAL, so New York hears 00:00, not 20:00.
    Low: the proxy always sends Z today."""
    out = run("America/New_York", ["2026-09-25T00:00:00", "2026-09-25T00:00:00+00:00"],
              tmp_path)
    assert out["2026-09-25T00:00:00+00:00"]["t"] == "20:00"
    assert out["2026-09-25T00:00:00"]["t"] == "20:00", out


def test_malformed_resets_at_is_still_said_in_her_language(tmp_path):
    """FAILURE. A missing or malformed resets_at makes local_hhmm return the English
    word "midnight", which is then dropped into the Hebrew/Russian/Arabic sentence:
    "הן חוזרות ב-midnight". It is also wrong for anyone not on UTC: the proxy's
    day rolls over at UTC midnight, which is 03:00 in Israel."""
    out = run("Asia/Jerusalem", ["", "garbage", "2026-13-45T00:00:00Z"], tmp_path)
    for iso, v in out.items():
        assert "midnight" not in v["he"] and "midnight" not in v["ru"], (iso, v)

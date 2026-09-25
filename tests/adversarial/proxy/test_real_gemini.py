"""The few behaviours only the real Gemini can show. Opt-in, and cheap:

    MICMIC_ADV_REAL=1 uv run python -m pytest ../tests/adversarial/proxy/test_real_gemini.py -s

The proxy process reads the real Gemini key from the workspace env file itself; this
file never reads, prints or stores it. Jev stays on the fake. Each test prints Gemini's
own usageMetadata so the spend is on the record: the whole file is a few thousand
flash-lite tokens, well under one cent.
"""
from __future__ import annotations

import json
import os

import pytest

from conftest import GEM_PATH, PROXY_PORT

pytestmark = pytest.mark.skipif(os.environ.get("MICMIC_ADV_REAL") != "1",
                                reason="real upstream calls are opt-in")

COUNT = "Count from 1 to 5000 in digits, separated by single spaces. No other text."


@pytest.fixture
def real(proxy_proc):
    p = proxy_proc(real_gemini=True, FREE_DAILY_GEMINI_CALLS=20)
    return p, p.mint()


def _call(p, tok, body):
    s, d, raw, _ = p.req("POST", GEM_PATH, body, tok, timeout=120)
    usage = (d or {}).get("usageMetadata") if isinstance(d, dict) else None
    cands = (d or {}).get("candidates") or [] if isinstance(d, dict) else []
    print(f"  status={s} candidates={len(cands)} "
          f"finish={[c.get('finishReason') for c in cands]} usage={json.dumps(usage)}"
          + ("" if s == 200 else f" body={raw[:200]!r}"))
    return s, d, usage or {}


def test_real_baseline_clamp_holds(real):
    """Control: the ordinary camelCase request is accepted."""
    p, tok = real
    s, _, u = _call(p, tok, {"contents": [{"parts": [{"text": COUNT}]}],
                             "generationConfig": {"maxOutputTokens": 50000}})
    assert s == 200 and u.get("candidatesTokenCount", 0) <= 1024


def test_real_snake_case_output_cap_bypass(real):
    """Guard. Observed: Gemini answers 400 to both spellings, because the proxy's injected
    generationConfig.maxOutputTokens makes the field appear twice. Refunded, no bypass."""
    p, tok = real
    results = {}
    for name, extra in (
            ("top-level generation_config",
             {"generation_config": {"max_output_tokens": 3000}}),
            ("snake field inside generationConfig",
             {"generationConfig": {"max_output_tokens": 3000}})):
        print(name)
        s, _, u = _call(p, tok, {"contents": [{"parts": [{"text": COUNT}]}], **extra})
        results[name] = (s, u.get("candidatesTokenCount", 0))
    print(results)
    assert all(n <= 1024 for s, n in results.values() if s == 200), results


def test_real_candidate_count_multiplies_output(real):
    """Guard. Observed: 400 from gemini-3.5-flash-lite for candidateCount 4."""
    p, tok = real
    s, d, u = _call(p, tok, {"contents": [{"parts": [{"text": COUNT}]}],
                             "generationConfig": {"candidateCount": 4}})
    assert s != 200 or u.get("candidatesTokenCount", 0) <= 1024, u


def test_real_file_uri_is_fetched_and_billed(real):
    """FAILURE, HIGH. A 19-second public YouTube video ("Me at the zoo"), referenced by
    URL from a 204-byte body. Observed promptTokenCount 1687 (VIDEO 1676): the owner
    pays for footage the proxy never saw."""
    p, tok = real
    body = {"contents": [{"parts": [
        {"fileData": {"fileUri": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}},
        {"text": "In five words, what animal is in this video?"}]}],
        "generationConfig": {"maxOutputTokens": 20}}
    print(f"  request body {len(json.dumps(body))} bytes")
    s, d, u = _call(p, tok, body)
    assert s != 200 or u.get("promptTokenCount", 0) < 1000, u

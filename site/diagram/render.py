"""Render the README "How it works" diagram, one PNG per colour scheme.

    uv run python site/diagram/render.py docs/assets

how-it-works.html is the source: edit it, then re-run this. The page is captured at 2x
with a transparent window, so the rounded corners sit cleanly on GitHub's page in
either theme. The background is flat on purpose: it keeps each PNG lossless and under
BUDGET. A gradient backdrop doubles the size, and a palette reduction then bands it.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE = (Path(__file__).resolve().parent / "how-it-works.html").as_uri()
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/assets")
OUT.mkdir(parents=True, exist_ok=True)
BUDGET = 250_000


def shrink(src, dst):
    """Keep the lossless capture if it fits BUDGET, else a 256-colour palette via ffmpeg."""
    if src.stat().st_size <= BUDGET or not shutil.which("ffmpeg"):
        shutil.copyfile(src, dst)
        return
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex",
                    "[0]split[a][b];[a]palettegen=max_colors=256:reserve_transparent=1:stats_mode=full[p];"
                    "[b][p]paletteuse=dither=sierra2_4a:alpha_threshold=128",
                    str(dst)], check=True)


with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
    browser = p.chromium.launch()
    for scheme in ("dark", "light"):
        ctx = browser.new_context(viewport={"width": 800, "height": 600}, color_scheme=scheme,
                                  device_scale_factor=2)
        page = ctx.new_page()
        page.goto(PAGE)
        page.evaluate("document.fonts.ready")
        page.evaluate("window.draw()")
        raw = Path(tmp) / f"{scheme}.png"
        page.locator("#stage").screenshot(path=str(raw), omit_background=True)
        dst = OUT / f"how-it-works-{scheme}.png"
        shrink(raw, dst)
        size = dst.stat().st_size
        print(dst, size, "bytes", "(over budget)" if size > BUDGET else "")
        ctx.close()
    browser.close()

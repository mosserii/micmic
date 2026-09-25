# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright==1.63.0"]
# ///
"""Render background.html into the disk image window's background.

Writes background.tiff: a 1x and a 2x (Retina) rendering in one file, so Finder picks
the sharp one on a Retina screen. The TIFF is committed; run this again only after
editing background.html:

    uv run native/dmg/render.py

Drives the installed Google Chrome through Playwright (no browser download). Plain
`chrome --headless --screenshot` writes the file and then never exits on this machine.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
W, H = 660, 420


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    shots = [tmp / "background.png", tmp / "background@2x.png"]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for scale, shot in zip((1, 2), shots):
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=scale)
            page.goto((HERE / "background.html").as_uri())
            # Rubik comes from a local file; without the wait the first shot can be
            # taken in the fallback font.
            page.evaluate("document.fonts.ready.then(() => document.fonts.size)")
            if not page.evaluate("document.fonts.check('18px Rubik')"):
                sys.exit("FAILED: Rubik did not load (proxy/web/static/fonts)")
            page.screenshot(path=str(shot))
            page.close()
        browser.close()
    # tiffutil refuses a pair that is not exactly 1:2, which is the check we want.
    r = subprocess.run(["/usr/bin/tiffutil", "-cathidpicheck", *map(str, shots),
                        "-out", str(HERE / "background.tiff")], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAILED: tiffutil exit {r.returncode}\n{r.stdout}{r.stderr}")
    for shot in shots:
        shot.unlink()
    tmp.rmdir()
    print(f"wrote {HERE / 'background.tiff'}")


if __name__ == "__main__":
    main()

"""Render the link-preview image (1200x630) from the live home page, dark mode.

It is the hero itself: the app icon with the MicMic name beside it, "Talk to your
Mac.", and the MicMic bar in its finished state (reduced motion), with the hint under
it. The Download button is left out: nothing in a preview can be clicked.
Run: .venv/bin/python3 site/og/render_og.py http://127.0.0.1:<port>/ <out dir>
then: sips -z 630 1200 -s format jpeg -s formatOptions 90 <out>/og-dark@2x.png --out proxy/web/static/og.jpg
(site/og/card.html is the earlier name-only card, kept as an alternative.)
"""
import sys
from playwright.sync_api import sync_playwright

url, out = sys.argv[1], sys.argv[2]
CSS = """
header, .site-head, nav, .ha-cta, main > section:not(.ha), footer { display: none !important; }
html, body { overflow: hidden !important; }
.ha { min-height: 630px !important; height: 630px !important; padding: 0 !important;
      display: flex !important; align-items: center !important; justify-content: center !important; }
.ha-in { transform: scale(1.06); transform-origin: center; }
.og-brand { display: flex; align-items: center; justify-content: center; gap: 16px; margin-bottom: 26px; }
.og-brand .ha-icon { margin: 0 !important; }
.og-name { font-size: 40px; font-weight: 700; letter-spacing: -.02em; color: var(--ink); }
"""
JS = """
const icon = document.querySelector('.ha-icon');
const row = document.createElement('div'); row.className = 'og-brand';
icon.parentNode.insertBefore(row, icon); row.appendChild(icon);
const name = document.createElement('span'); name.className = 'og-name'; name.textContent = 'MicMic';
row.appendChild(name);
"""
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1200, "height": 630}, device_scale_factor=2,
                    color_scheme="dark", reduced_motion="reduce")
    pg.goto(url); pg.wait_for_timeout(1200)
    pg.add_style_tag(content=CSS); pg.evaluate(JS); pg.wait_for_timeout(600)
    pg.screenshot(path=f"{out}/og-dark@2x.png")
    b.close()

#!/usr/bin/env python3
"""Render MicMic's menu-bar icon assets from the hand-simplified SVGs in this
folder (mic-idle.svg, mic-active.svg — a reduction of ../micmic-mark.svg's
twin-capsule-on-a-stand mark to a silhouette that stays legible at 16px).

    uv run python brand/menubar/render.py

Writes the template PNGs native/listener.py loads at runtime:
    mic-idle.png, mic-idle@2x.png, mic-active.png, mic-active@2x.png
(18x18 pt / 36x36 px, pure black on transparent — macOS recolors a template
image itself, so no other color ever appears in these files), plus four
4x-zoomed preview PNGs on a light and a dark menu-bar-colored swatch, for a
human to actually look at before this ships:
    preview-idle-light.png    preview-idle-dark.png
    preview-hearing-light.png preview-hearing-dark.png

It also builds a side-by-side weight comparison against the system "mic" SF
Symbol at the same size (run sf_symbol_ref.py first -- see below):
    preview-compare-light.png preview-compare-dark.png
(SF mic, then MicMic idle, then MicMic hearing, left to right.)

It also renders orb-mic.svg -- the same mark, redrawn for the app orb's 24x24
glyph (savta/web/index.html's #core svg and onboarding.js's SVG.mic, which
embed this path inline and are kept in sync with orb-mic.svg by hand) -- at
4x zoom in every color/opacity the orb actually uses (see index.html's
body.idle/listening/thinking/acting/error rules), light and dark:
    orb-idle-light.png      orb-idle-dark.png
    orb-listening-light.png orb-listening-dark.png
    orb-thinking-light.png  orb-thinking-dark.png
    orb-acting-light.png    orb-acting-dark.png
    orb-error-light.png     orb-error-dark.png

Playwright, not the headless-chrome CLI (which hangs in this environment).
Needs the chromium browser Playwright already downloaded for
tests/layout/test_layout.py; if it is missing, `uv run playwright install
chromium` first.

The SF Symbol comparison needs sf-mic-ref@2x.png, which only AppKit (PyObjC) can
render, so it is a separate one-time step run under native/.venv, not this
Playwright venv: `native/.venv/bin/python3 brand/menubar/sf_symbol_ref.py`. If
that file is missing, this script skips the comparison preview and says so.
"""
from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

ICONS = {"mic-idle": "idle", "mic-active": "hearing"}
# macOS System Settings' own approximate menu-bar chrome colors, light and dark.
SWATCHES = {"light": ("#e4e4e6", "#1d1d1f"), "dark": ("#242426", "#f4f4f5")}

# The orb's own ground color and each state's glyph color/opacity, lifted straight
# from savta/web/index.html's :root and body.<state> .orb .core rules, so these
# previews show exactly what the app draws, not an approximation of it.
ORB_GROUND = {"light": "#efeae3", "dark": "#14110e"}
ORB_STATES = {
    #            light color, light opacity, dark color, dark opacity
    "idle":      ("#584e44", .6,  "#bcb1a5", .6),
    "listening": ("#2d8b6b", 1.0, "#57c69d", 1.0),
    "thinking":  ("#bd7a17", 1.0, "#e0a441", 1.0),
    "acting":    ("#bd7a17", 1.0, "#e0a441", 1.0),
    "error":     ("#b5544a", 1.0, "#ee8b7f", 1.0),
}


def _icon_html(svg: str) -> str:
    return (f"<!doctype html><html><head><style>"
            f"html,body{{margin:0;padding:0;background:transparent;}}"
            f"svg{{display:block;}}</style></head><body>{svg}</body></html>")


def _preview_html(svg: str, bg: str, fg: str) -> str:
    # Same silhouette, recolored the way macOS would paint a template image:
    # near-black on the light menu bar, near-white on the dark one.
    svg = svg.replace("#000000", fg)
    return (f"<!doctype html><html><head><style>"
            f"html,body{{margin:0;padding:0;}}"
            f".bar{{width:160px;height:120px;background:{bg};"
            f"display:flex;align-items:center;justify-content:center;}}"
            f"svg{{width:72px;height:72px;}}</style></head>"
            f"<body><div class=\"bar\">{svg}</div></body></html>")


def _compare_html(sf_png_b64: str, idle_svg: str, active_svg: str, bg: str, fg: str,
                   dark: bool) -> str:
    idle_svg = idle_svg.replace("#000000", fg)
    active_svg = active_svg.replace("#000000", fg)
    # The SF Symbol PNG is fixed black-on-transparent; on the dark swatch it needs
    # inverting to white, exactly like a real template image would be repainted.
    sf_filter = "filter:invert(1);" if dark else ""
    cell = ('display:flex;flex-direction:column;align-items:center;gap:6px;'
            f'color:{fg};font:11px -apple-system,sans-serif;')
    return (f"<!doctype html><html><head><style>"
            f"html,body{{margin:0;padding:0;}}"
            f".bar{{width:340px;height:130px;background:{bg};"
            f"display:flex;align-items:center;justify-content:center;gap:36px;}}"
            f".cell{{{cell}}}"
            f".cell img,.cell svg{{width:64px;height:64px;}}"
            f".cell img{{{sf_filter}}}"
            f"</style></head><body><div class=\"bar\">"
            f"<div class=\"cell\"><img src=\"data:image/png;base64,{sf_png_b64}\"><span>SF mic</span></div>"
            f"<div class=\"cell\">{idle_svg}<span>MicMic idle</span></div>"
            f"<div class=\"cell\">{active_svg}<span>MicMic hearing</span></div>"
            f"</div></body></html>")


def _orb_html(svg: str, ground: str, color: str, opacity: float) -> str:
    # fill=currentColor already on the svg root; setting color on the wrapper is
    # exactly what body.<state> .orb .core{color:...} does in the real app.
    return (f"<!doctype html><html><head><style>"
            f"html,body{{margin:0;padding:0;}}"
            f".bar{{width:160px;height:120px;background:{ground};"
            f"display:flex;align-items:center;justify-content:center;}}"
            f".core{{color:{color};opacity:{opacity};width:72px;height:72px;}}"
            f".core svg{{width:100%;height:100%;}}</style></head>"
            f"<body><div class=\"bar\"><div class=\"core\">{svg}</div></div></body></html>")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for stem, label in ICONS.items():
                svg = (HERE / f"{stem}.svg").read_text()
                for scale, suffix in ((1, ""), (2, "@2x")):
                    icon_page = browser.new_page(viewport={"width": 18, "height": 18},
                                                  device_scale_factor=scale)
                    icon_page.set_content(_icon_html(svg))
                    out = HERE / f"{stem}{suffix}.png"
                    icon_page.locator("svg").screenshot(path=str(out), omit_background=True)
                    icon_page.close()
                    print(f"wrote {out.relative_to(HERE.parents[1])}")
                for scheme, (bg, fg) in SWATCHES.items():
                    prev_page = browser.new_page(viewport={"width": 160, "height": 120})
                    prev_page.set_content(_preview_html(svg, bg, fg))
                    out = HERE / f"preview-{label}-{scheme}.png"
                    prev_page.locator(".bar").screenshot(path=str(out))
                    prev_page.close()
                    print(f"wrote {out.relative_to(HERE.parents[1])}")

            sf_ref = HERE / "sf-mic-ref@2x.png"
            if sf_ref.exists():
                sf_b64 = base64.b64encode(sf_ref.read_bytes()).decode()
                idle_svg = (HERE / "mic-idle.svg").read_text()
                active_svg = (HERE / "mic-active.svg").read_text()
                for scheme, (bg, fg) in SWATCHES.items():
                    cmp_page = browser.new_page(viewport={"width": 340, "height": 130})
                    cmp_page.set_content(_compare_html(sf_b64, idle_svg, active_svg, bg, fg,
                                                        dark=(scheme == "dark")))
                    out = HERE / f"preview-compare-{scheme}.png"
                    cmp_page.locator(".bar").screenshot(path=str(out))
                    cmp_page.close()
                    print(f"wrote {out.relative_to(HERE.parents[1])}")
            else:
                print("skipping preview-compare-*: run "
                      "'native/.venv/bin/python3 brand/menubar/sf_symbol_ref.py' first")

            orb_svg = (HERE / "orb-mic.svg").read_text()
            for state, (lc, lo, dc, do) in ORB_STATES.items():
                for scheme, ground, color, opacity in (
                        ("light", ORB_GROUND["light"], lc, lo),
                        ("dark", ORB_GROUND["dark"], dc, do)):
                    orb_page = browser.new_page(viewport={"width": 160, "height": 120})
                    orb_page.set_content(_orb_html(orb_svg, ground, color, opacity))
                    out = HERE / f"orb-{state}-{scheme}.png"
                    orb_page.locator(".bar").screenshot(path=str(out))
                    orb_page.close()
                    print(f"wrote {out.relative_to(HERE.parents[1])}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()

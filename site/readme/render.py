"""Render the README art from the real bar: the animated hero, the bar screenshots
and the GitHub social preview.

    MICMIC_PORT=8891 MICMIC_STATE_DIR=$(mktemp -d) .venv/bin/python3 -m savta.server &
    .venv/bin/python3 site/readme/render.py http://127.0.0.1:8891 docs/assets

Everything the bar shows is driven through the same calls native/bar.py makes
(barState, barHeard, barResult) on savta/web/index.html?bar=1, so the pictures are the
shipped UI, not a drawing of it. hero.html only adds the backdrop, the name and the
key hint around it. Every POST from the page is answered here and never reaches the
server: /api/duck would turn the real volume down. The phrases and replies are the
router's own strings for things MicMic does. Needs ffmpeg for the GIF.
"""
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2])
HERO = (Path(__file__).resolve().parent / "hero.html").as_uri()
OUT.mkdir(parents=True, exist_ok=True)

# The page talks to native through webkit.messageHandlers and would ask for the
# microphone outside bar mode; both are stubbed so nothing real is touched.
INIT = """
window.webkit = {messageHandlers: {micmicbar: {postMessage: () => 0}, micmic: {postMessage: () => 0}}};
class FakeSR { start(){} stop(){} abort(){} }
window.SpeechRecognition = FakeSR; window.webkitSpeechRecognition = FakeSR;
try{ localStorage.setItem('micmic.lang', 'en'); }catch(_){}
"""

# (language, what you say, what the bar answers, undo label or None). The replies are
# the router's strings: timer_set, playing_t, the selection send, and the Hebrew timer.
SCENES = [
    ("en", "Remind me in ten minutes to call Mom", "I will remind you in 10 minutes.", ""),
    ("en", "Play this week’s top 100 hits", "Here you go. Top 100 Songs This Week.", ""),
    ("en", "Send this to Matan", "Sending Matan what you selected. Say no and I will stop.", None),
    ("he", "תזכירי לי בעוד עשר "
           "דקות להתקשר לאמא",
     "אזכיר לך בעוד 10 דקות.", ""),
]


GITHUB_BG = {"light": "#ffffff", "dark": "#0d1117"}


def guard(route):
    if route.request.method == "POST":
        route.fulfill(status=200, content_type="application/json", body="{}")
    else:
        route.continue_()


class Bar:
    """The bar page at its native width, captured at `dpr` with a transparent window."""

    def __init__(self, browser, scheme, dpr):
        self.ctx = browser.new_context(viewport={"width": 640, "height": 64}, color_scheme=scheme,
                                       device_scale_factor=dpr, reduced_motion="reduce")
        self.ctx.route("**/api/**", guard)
        self.ctx.add_init_script(INIT)
        self.page = self.ctx.new_page()
        self.page.goto(BASE + "/?bar=1")
        self.page.wait_for_function("() => !!window.barState")
        self.page.evaluate("document.fonts.ready")

    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def lang(self, code):
        self.js("c => applyLanguage(c, {remember: false})", code)

    def result(self, scene):
        code, said, reply, undo = scene
        self.lang(code)
        self.js("barState('listening', '')")
        self.js("t => barHeard(t)", said)
        self.js("([r, u]) => barResult(r, u)", [reply, undo])

    def png(self, path=None):
        """What native does with the bar's height message: make the window that tall."""
        h = self.js("Math.ceil(document.querySelector('.stage').getBoundingClientRect().height)")
        if h != self.page.viewport_size["height"]:
            self.page.set_viewport_size({"width": 640, "height": max(h, 40)})
        self.page.wait_for_timeout(60)
        return self.page.screenshot(path=path, omit_background=True)

    def close(self):
        self.ctx.close()


class Hero:
    def __init__(self, browser, scheme, w, h, scale, social=False, page=None):
        self.ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme,
                                       device_scale_factor=2, reduced_motion="reduce")
        self.page = self.ctx.new_page()
        self.page.goto(HERO)
        self.page.evaluate("([w, h, s, social]) => { const st = document.getElementById('stage');"
                           " st.style.setProperty('--w', w + 'px'); st.style.setProperty('--h', h + 'px');"
                           " st.style.setProperty('--s', s); document.getElementById('sub').hidden = !social; }",
                           [w, h, scale, social])
        if page:
            self.page.evaluate("c => document.documentElement.style.setProperty('--page', c)", page)
        self.page.evaluate("document.fonts.ready")
        # Captured at the size it is shown, so it lands pixel for pixel.
        self.bar = Bar(browser, scheme, 2 * scale)

    def show(self, held=None):
        src = "data:image/png;base64," + base64.b64encode(self.bar.png()).decode()
        self.page.evaluate("s => new Promise(ok => { const i = document.getElementById('bar');"
                           " i.onload = ok; i.src = s; })", src)
        if held is not None:
            self.page.evaluate("on => document.getElementById('stage').classList.toggle('held', on)", held)

    def shot(self, path):
        self.page.screenshot(path=str(path))

    def close(self):
        self.bar.close()
        self.ctx.close()


def stills(browser):
    for scheme in ("light", "dark"):
        # The bar alone, in each state, the size native shows it.
        bar = Bar(browser, scheme, 2)
        bar.js("barState('listening', '')")
        bar.js("barHeard('Add this to my calendar')")
        bar.png(str(OUT / f"bar-listening-{scheme}.png"))
        bar.js("barState('thinking', '')")
        bar.png(str(OUT / f"bar-thinking-{scheme}.png"))
        bar.js("barResult('Shall I add Flight to Lisbon to your calendar, Friday 2 October at 07:40?', null)")
        bar.png(str(OUT / f"bar-result-{scheme}.png"))
        bar.result(SCENES[3])
        bar.png(str(OUT / f"bar-hebrew-{scheme}.png"))
        bar.close()

    # GitHub's social preview: 1280x640, dark, the finished bar.
    hero = Hero(browser, "dark", 1280, 640, 1.45, social=True)
    hero.bar.result(SCENES[1])
    hero.show()
    hero.shot(OUT / "social-preview@2x.png")  # then: sips -z 640 1280 ... .github/social-preview.png
    hero.close()


def loop(browser, scheme, tmp):
    """One pass through SCENES as (png, seconds) frames, then a palette-matched GIF."""
    # GitHub's own page colours, so the hero has no edge in the README.
    hero = Hero(browser, scheme, 880, 440, 1.2, page=GITHUB_BG[scheme])
    bar, frames = hero.bar, []

    def frame(seconds, held=None):
        hero.show(held)
        p = tmp / f"{scheme}-{len(frames):04d}.png"
        hero.shot(p)
        frames.append((p, seconds))

    for code, said, reply, undo in SCENES:
        bar.lang(code)
        bar.js("barState('listening', '')")
        frame(0.45, held=True)
        words = said.split(" ")
        for i in range(1, len(words) + 1):
            bar.js("t => barHeard(t)", " ".join(words[:i]))
            frame(0.5 if i == len(words) else 0.17)
        bar.js("barState('thinking', '')")
        frame(0.9, held=False)
        bar.js("([r, u]) => barResult(r, u)", [reply, undo])
        frame(2.6)
    hero.close()

    lst = tmp / f"{scheme}.txt"
    lst.write_text("".join(f"file '{p}'\nduration {s}\n" for p, s in frames)
                   + f"file '{frames[-1][0]}'\n")
    gif = OUT / f"hero-{scheme}.gif"
    # 1320 wide: GitHub shows the hero at up to ~830 px, so this is sharp on a Retina
    # screen, and the frames were captured at 1760 so the downscale is clean.
    vf = ("scale=1320:-1:flags=lanczos,split[a][b];"
          "[a]palettegen=max_colors=256:stats_mode=full[p];"
          "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", vf, "-fps_mode", "vfr", "-loop", "0", str(gif)], check=True)
    return gif


def main():
    tmp = Path(tempfile.mkdtemp(prefix="micmic-readme-"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        stills(browser)
        for scheme in ("light", "dark"):
            gif = loop(browser, scheme, tmp)
            print(gif, f"{gif.stat().st_size / 1e6:.2f} MB")
        browser.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()

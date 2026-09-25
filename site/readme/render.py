"""Render the README art from the real bar: the animated hero, the bar screenshots
and the GitHub social preview.

    MICMIC_PORT=8891 MICMIC_STATE_DIR=$(mktemp -d) .venv/bin/python3 -m savta.server &
    .venv/bin/python3 site/readme/render.py http://127.0.0.1:8891 docs/assets
    .venv/bin/python3 site/readme/render.py http://127.0.0.1:8891 docs/assets hero   # GIFs only

The hero is one real browser-agent run (FLIGHT): what was said, the reply the router
gave, how long it took, and docs/assets/flight-final.png, the page the agent stopped
on, captured from that run. Re-record all four together or none of them.

Everything the bar shows is driven through the same calls native/bar.py makes
(barState, barHeard, barResult) on savta/web/index.html?bar=1, so the pictures are the
shipped UI, not a drawing of it. hero.html only adds the backdrop, the name, the
key hint, the tagline and the plain window the agent's screenshot sits in. Every POST from the page is answered here and never reaches the
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
# the router's strings: timer_set, playing_t, and the selection send. English only:
# the public README shows no other language.
SCENES = [
    ("en", "Remind me in ten minutes to call Mom", "I will remind you in 10 minutes.", ""),
    ("en", "Play this week’s top 100 hits", "Here you go. Top 100 Songs This Week.", ""),
    ("en", "Send this to Matan", "Sending Matan what you selected. Say no and I will stop.", None),
]


# One real run of the do_online branch (router.py) on 2026-09-25, headless, fresh
# profile: Google Flights, one way, TLV to LIS on Fri 2 Oct ("next Friday", typed as
# "Oct 2, 2026"), 20 agent steps, 35 s from the words to the answer. "Found it." is
# the router's `done` line.
FLIGHT = {"said": "Find me a flight from Tel Aviv to Lisbon next Friday",
          "reply": "Found it. It is on the screen.", "seconds": 35,
          "page": "google.com/travel/flights", "shot": "flight-final.png"}

GITHUB_BG = {"light": "#ffffff", "dark": "#0d1117"}


def guard(route):
    if route.request.method == "POST":
        route.fulfill(status=200, content_type="application/json", body="{}")
    else:
        route.continue_()


class Bar:
    """The bar page at its native width, captured at `dpr` with a transparent window."""

    def __init__(self, browser, scheme, dpr, clock=False):
        self.ctx = browser.new_context(viewport={"width": 640, "height": 64}, color_scheme=scheme,
                                       device_scale_factor=dpr, reduced_motion="reduce")
        self.ctx.route("**/api/**", guard)
        self.ctx.add_init_script(INIT)
        self.page = self.ctx.new_page()
        if clock:
            # A fake clock, so the bar's own "Working · Ns" counter can be walked
            # through the real run's seconds without waiting them out.
            self.page.clock.install()
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
    def __init__(self, browser, scheme, w, h, scale, social=False, page=None, clock=False):
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
        self.bar = Bar(browser, scheme, 2 * scale, clock)

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
        bar.close()

    # GitHub's social preview: 1280x640, dark, the finished bar.
    hero = Hero(browser, "dark", 1280, 640, 1.45, social=True)
    hero.bar.result(SCENES[1])
    hero.show()
    hero.shot(OUT / "social-preview@2x.png")  # then: sips -z 640 1280 ... .github/social-preview.png
    hero.close()


def loop(browser, scheme, tmp):
    """FLIGHT as (png, seconds) frames, then a palette-matched GIF."""
    # GitHub's own page colours, so the hero has no edge in the README.
    # The tagline too, as on the social preview, so the one looping picture says what
    # MicMic is and what understands you.
    hero = Hero(browser, scheme, 880, 620, 1.2, social=True, page=GITHUB_BG[scheme], clock=True)
    bar, frames = hero.bar, []
    hero.page.evaluate("([s, a]) => new Promise(ok => { const i = document.getElementById('shot');"
                       " document.getElementById('addr').textContent = a; i.onload = ok; i.src = s; })",
                       [(OUT / FLIGHT["shot"]).resolve().as_uri(), FLIGHT["page"]])

    def frame(seconds, held=None):
        hero.show(held)
        p = tmp / f"{scheme}-{len(frames):04d}.png"
        hero.shot(p)
        frames.append((p, seconds))

    bar.lang("en")
    bar.js("barState('listening', '')")
    frame(0.45, held=True)
    words = FLIGHT["said"].split(" ")
    for i in range(1, len(words) + 1):
        bar.js("t => barHeard(t)", " ".join(words[:i]))
        frame(0.5 if i == len(words) else 0.17)
    # The bar's own counter, walked through the run's real seconds in a few ticks.
    # Paused while it counts: rendering a frame takes real time, which the counter
    # would otherwise add to the run's.
    bar.page.clock.pause_at(bar.js("Date.now()") + 1000)
    bar.js("barState('thinking', '')")
    frame(0.6, held=False)
    at = 0
    for sec in (5, 12, 19, 27, FLIGHT["seconds"]):
        bar.page.clock.run_for((sec - at) * 1000)
        at = sec
        frame(0.3)
    bar.js("([r, u]) => barResult(r, u)", [FLIGHT["reply"], None])
    bar.page.clock.resume()
    frame(1.3)
    # Few in-between frames: each one carries the whole screenshot, and it is those,
    # not the bar, that decide the size of the GIF.
    for p, seconds in ((.25, .08), (.5, .08), (.75, .08), (1, 4.2), (0, .3)):
        hero.page.evaluate("p => reveal(p)", p)
        frame(seconds)
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
        if "hero" not in sys.argv[3:]:
            stills(browser)
        for scheme in ("light", "dark"):
            gif = loop(browser, scheme, tmp)
            print(gif, f"{gif.stat().st_size / 1e6:.2f} MB")
        browser.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()

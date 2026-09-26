"""Generate MicMic logo candidates with Gemini's image model.

Adapted from ~/dev/verde-pitch/directions/gen_sharp.py: same endpoint, same key file,
same retry. Several directions per run and pick, rather than one and iterate.
Run:  cd ~/jev/savta/brand && ../.venv/bin/python3 gen_logo.py [name ...]
"""
import base64, json, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

KEY = None
for line in open(os.path.expanduser("~/dev/splash-backend/.env")):
    m = re.match(r"^GEMINI_API_KEY=(.*)$", line.strip())
    if m:
        KEY = m.group(1).strip().strip('"').strip("'")
assert KEY, "GEMINI_API_KEY not found in ~/dev/splash-backend/.env"

URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       "gemini-3-pro-image-preview:generateContent")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "candidates")

# Shared rules, stated every time: one subject, one background, the medium named,
# and "no text" said explicitly, which is what kept stray wordmarks out on Verde.
RULES = ("Flat vector-style logo mark, single solid colour on a pure white background, "
         "generous negative space, no gradients, no shadows, no outlines of a frame, "
         "no text, no letters, no wordmark, centred, 1:1.")

PROMPTS = {
    # the in-app identity: the glass orb with a microphone in it
    "a-orb": "A minimal logo mark: a perfect circle containing a simple microphone "
             "silhouette cut out as negative space, the microphone capsule rounded, a "
             "short stand, reduced to a few geometric shapes. Colour deep teal green "
             "#2d8b6b. " + RULES,
    # the name: two microphones, the doubling in MicMic
    "b-double": "A minimal logo mark made of two identical rounded microphone capsules "
                "standing side by side and slightly overlapping, sharing one stand, so "
                "together they read as a friendly pair. Geometric, balanced, playful but "
                "calm. Colour deep teal green #2d8b6b. " + RULES,
    # what it does: speaking turns into action
    "c-bubble": "A minimal logo mark: a rounded speech bubble whose tail at the bottom "
                "becomes the stand of a microphone, so the bubble itself is the "
                "microphone's head. Soft rounded geometry, friendly and warm. Colour "
                "warm amber #bd7a17. " + RULES,
    # the suggested shape from Verde, straight
    "d-studio": "Minimal flat logo mark of a studio microphone reduced to a few geometric "
                "shapes: a rounded capsule with three horizontal grille slots, a U-shaped "
                "cradle and a short stand. Colour deep teal green #2d8b6b. " + RULES,
}


# Second pass: the chosen candidate goes in as a reference, and the prompt asks for the
# same mark with its problems named. On Verde this converged faster than re-rolling.
REFINE = {
    "b2-clean": ("candidates/b-double.png",
        "Same logo mark as the reference: two rounded microphone capsules side by side "
        "sharing ONE stand. Refine the proportions: perfectly symmetric about the "
        "vertical centre line, both capsules identical in size, touching but not "
        "overlapping, ONE smooth U-shaped cradle hugging both capsules, ONE straight "
        "stand exactly on the centre line, uniform stroke weight throughout. Strictly "
        "one flat colour, deep teal green #2d8b6b, with NO darker overlap region. " + RULES),
    "b2-tight": ("candidates/b-double.png",
        "Same concept as the reference, simplified further into a bold icon that still "
        "reads at 32 pixels: two identical rounded microphone capsules touching side by "
        "side, one wide U-shaped cradle beneath both, one short stand and a flat base "
        "on the centre line. Thick, even strokes, symmetric, geometric. Strictly one "
        "flat colour, deep teal green #2d8b6b, no overlap shading. " + RULES),
    "b2-orb": ("candidates/b-double.png",
        "The same two-microphone mark as the reference, symmetric and simplified, cut "
        "out as white negative space inside a solid filled circle of deep teal green "
        "#2d8b6b: two identical rounded capsules touching side by side, one U-shaped "
        "cradle beneath both, one stand on the centre line. The circle fills most of "
        "the canvas. Flat, no gradients, no shading, no text, pure white outside the "
        "circle, centred, 1:1."),
}


def gen(name: str, prompt: str, ref: str | None = None) -> str:
    parts = [{"text": prompt}]
    if ref:
        data = base64.b64encode(open(os.path.join(HERE, ref), "rb").read()).decode()
        parts = [{"inlineData": {"mimeType": "image/png", "data": data}}] + parts
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": "1:1", "imageSize": "2K"}}}
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                URL, data=json.dumps(body).encode(),
                headers={"x-goog-api-key": KEY, "Content-Type": "application/json"})
            res = json.load(urllib.request.urlopen(req, timeout=300))
            for part in res.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    path = os.path.join(OUT, f"{name}.png")
                    open(path, "wb").write(base64.b64decode(part["inlineData"]["data"]))
                    return f"{name}: ok -> {path}"
            return f"{name}: no image in response"
        except urllib.error.HTTPError as e:
            msg = e.read()[:160]
            if attempt == 2:
                return f"{name}: HTTP {e.code} {msg!r}"
            time.sleep(15)
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return f"{name}: {e!r}"
            time.sleep(10)
    return f"{name}: failed"


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    names = sys.argv[1:] or list(PROMPTS)

    def one(n: str) -> str:
        if n in REFINE:
            ref, prompt = REFINE[n]
            return gen(n, prompt, ref)
        return gen(n, PROMPTS[n])

    with ThreadPoolExecutor(max_workers=4) as pool:
        for line in pool.map(one, names):
            print(line, flush=True)

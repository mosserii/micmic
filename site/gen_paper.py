"""Makes the paper grain tiles hero B lays over its canvas.

    uv run --no-project --with pillow python site/gen_paper.py

Writes proxy/web/static/paper-light.webp (dark flecks, for a pale page) and
paper-dark.webp (pale flecks, for a dark one). Both tile seamlessly: every blur is
done on a 3x3 wrap of the tile and cropped back to the middle, so the edges match.
Seeded, so a re-run gives the same bytes."""
import random
from pathlib import Path

from PIL import Image, ImageFilter

N = 256
OUT = Path(__file__).resolve().parent.parent / "proxy" / "web" / "static"


def wrap_blur(im: Image.Image, r: float) -> Image.Image:
    big = Image.new(im.mode, (N * 3, N * 3))
    for x in range(3):
        for y in range(3):
            big.paste(im, (x * N, y * N))
    return big.filter(ImageFilter.GaussianBlur(r)).crop((N, N, 2 * N, 2 * N))


def field(rnd: random.Random, r: float) -> Image.Image:
    im = Image.new("L", (N, N))
    im.putdata([rnd.randrange(256) for _ in range(N * N)])
    return wrap_blur(im, r) if r else im


def main() -> None:
    rnd = random.Random(7)
    fine = field(rnd, 0.6)       # the tooth of the paper
    cloud = field(rnd, 14)        # slow unevenness in how the fibres lie
    fibres = Image.new("L", (N, N), 0)
    px = fibres.load()
    for _ in range(70):         # short, faint strands
        x, y = rnd.randrange(N), rnd.randrange(N)
        dx, dy = rnd.uniform(-1, 1), rnd.uniform(-.35, .35)
        for t in range(rnd.randrange(6, 22)):
            px[int(x + dx * t) % N, int(y + dy * t) % N] = 255
    fibres = wrap_blur(fibres, 0.7)
    for name, rgb, gain in (("paper-light.webp", (60, 50, 35), 1.0), ("paper-dark.webp", (255, 250, 240), .8)):
        alpha = []
        for f, c, s in zip(fine.tobytes(), cloud.tobytes(), fibres.tobytes()):
            a = abs(f - 128) * .13 + max(0, c - 118) * .30 + s * .045
            alpha.append(max(0, min(255, int(a * gain))))
        tile = Image.new("RGBA", (N, N), rgb + (0,))
        tile.putalpha(Image.frombytes("L", (N, N), bytes(alpha)))
        tile.save(OUT / name, quality=72, method=6)
        print(name, (OUT / name).stat().st_size)


if __name__ == "__main__":
    main()

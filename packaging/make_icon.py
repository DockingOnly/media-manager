"""Draw the app icon (the violet-to-pink rounded square from the app bar) as packaging/icon.ico.
Run by build.bat when icon.ico is missing; needs Pillow, which the app already uses."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 256
top, bottom = (165, 148, 255), (255, 143, 177)  # the accent and the pink end of the app bar's mark
grad = Image.new("RGBA", (S, S))
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * (S - 1))  # diagonal, like the CSS 135deg gradient
        px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([8, 8, S - 9, S - 9], radius=64, fill=255)
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)
# a simple play mark, so it reads as a media app at small sizes
ImageDraw.Draw(icon).polygon([(100, 78), (100, 178), (182, 128)], fill=(255, 255, 255, 235))
icon.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", HERE / "icon.ico")

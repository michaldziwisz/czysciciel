#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generator ikony Czysciciela: okragly gradient + fala dzwieku + iskra czystosci.
Rysuje w 1024px (SSAA), zapisuje wielorozmiarowy .ico + podglad .png."""
import math
from PIL import Image, ImageDraw

S = 1024                      # plotno robocze (potem downscale = gladkie krawedzie)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# --- tlo: okragly pionowy gradient (glebki granat -> zywy blekit) ---
top = (34, 92, 214)           # #225CD6
bot = (14, 32, 84)            # #0E2054
r = S // 2
cx = cy = r
circle = Image.new("RGBA", (S, S), (0, 0, 0, 0))
cd = ImageDraw.Draw(circle)
for y in range(S):
    t = y / S
    col = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,)
    cd.line([(0, y), (S, y)], fill=col)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).ellipse([6, 6, S - 6, S - 6], fill=255)
img.paste(circle, (0, 0), mask)

# subtelna obwodka
d.ellipse([6, 6, S - 6, S - 6], outline=(255, 255, 255, 60), width=8)

# --- fala dzwieku: symetryczne slupki (equalizer) w bieli ---
# mniej, grubszych slupkow = czytelne takze w 16-32px
bars = [0.42, 0.72, 1.00, 0.64, 0.86, 0.52, 0.34]
n = len(bars)
span = S * 0.60
bw = span / (n * 1.55)
gap = bw * 0.55
total = n * bw + (n - 1) * gap
x0 = cx - total / 2
maxh = S * 0.40
for i, b in enumerate(bars):
    h = maxh * b
    x = x0 + i * (bw + gap)
    y1 = cy - h / 2
    y2 = cy + h / 2
    d.rounded_rectangle([x, y1, x + bw, y2], radius=bw / 2,
                        fill=(240, 248, 255, 255))

# --- iskra czystosci (4-ramienna gwiazdka) w prawym gornym rogu ---
def sparkle(dc, sx, sy, R, col):
    r2 = R * 0.34
    pts = []
    for k in range(8):
        ang = math.pi / 2 - k * math.pi / 4
        rad = R if k % 2 == 0 else r2
        pts.append((sx + rad * math.cos(ang), sy - rad * math.sin(ang)))
    dc.polygon(pts, fill=col)

sparkle(d, S * 0.735, S * 0.265, S * 0.115, (255, 236, 150, 255))  # duza, zlota
sparkle(d, S * 0.83, S * 0.40, S * 0.055, (255, 255, 255, 235))    # mala, biala

# --- downscale do gladkiego 256 i zapis .ico wielorozmiarowego ---
base = img.resize((256, 256), Image.LANCZOS)
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
base.save("assets/czysciciel.ico", sizes=sizes)
base.save("assets/czysciciel_podglad.png")
print("OK: assets/czysciciel.ico + assets/czysciciel_podglad.png")

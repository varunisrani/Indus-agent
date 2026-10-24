# Contact sheets for review: python3 sheet.py <png dir> <out dir>  (light|dark pairs side by side)
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw
src, out = Path(sys.argv[1]), Path(sys.argv[2])
shots = json.loads((src / "manifest.json").read_text())["shots"]
per = 3
for i in range(0, len(shots), per):
    sheet = Image.new("RGB", (1440, 450 * per + 20 * per), "white")
    d = ImageDraw.Draw(sheet)
    for j, s in enumerate(shots[i:i + per]):
        y = j * 470
        d.text((4, y + 2), s["slug"], fill="red")
        for k, t in enumerate(("light", "dark")):
            im = Image.open(src / s[t]).convert("RGB").resize((720, 450))
            sheet.paste(im, (k * 720, y + 20))
    sheet.save(out / f"sheet-{i // per:02d}.png")

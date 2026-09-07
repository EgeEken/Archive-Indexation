import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from photo_select import ROOT


def sheet(items, output, title, columns=5):
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 15)
    heading = ImageFont.truetype(str(font_path), 23)
    cell_w, cell_h = 280, 224
    canvas = Image.new("RGB", (columns * cell_w, 60 + ((len(items) + columns - 1) // columns) * cell_h), "#15191d")
    draw = ImageDraw.Draw(canvas)
    draw.text((15, 14), title, font=heading, fill="white")
    for i, (path, label) in enumerate(items):
        x, y = (i % columns) * cell_w, 60 + (i // columns) * cell_h
        with Image.open(path) as im:
            im.draft("RGB", (512, 512))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((cell_w - 10, cell_h - 40))
            canvas.paste(im, (x + (cell_w - im.width) // 2, y + (cell_h - 40 - im.height) // 2))
        draw.text((x + 6, y + cell_h - 34), label, font=font, fill="white")
    with Path(output).open("xb") as f:
        canvas.save(f, format="JPEG", quality=88)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual-development", action="store_true")
    args = parser.parse_args()
    if args.manual_development:
        inv = json.loads((ROOT / "results/archive_inventory_before.json").read_text(encoding="utf-8"))
        sessions = [s for s in inv["sessions"] if any(k in s["name"] for k in ["konser", "yurt", "montsouris", "üsküdar"])]
        items = []
        for s in sessions:
            pairs = s["matches"]
            for i in range(5):
                p = pairs[min(len(pairs) - 1, i * len(pairs) // 5)]["source"]
                items.append((p, f'{s["name"][:10]} · {Path(p).name}'))
        sheet(items, ROOT / "results/development_manual_examples.jpg", "Manual picks · development sessions only")

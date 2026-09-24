"""Generate the committed JPEG XL profile preview artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from io import BytesIO
from pathlib import Path

import imagecodecs
import numpy as np
from PIL import Image, ImageChops, ImageStat

from archive_index.file_management import quality_to_distance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reference_bytes = args.reference.read_bytes()
    reference = Image.open(BytesIO(reference_bytes)).convert("RGB")
    try:
        entries = []
        for slug, name, quality in (("high-quality", "JXL High Quality", 80), ("balanced", "JXL Balanced", 60), ("high-compression", "JXL High Compression", 40)):
            encoded = imagecodecs.jpegxl_encode(np.asarray(reference), distance=quality_to_distance(quality), effort=7)
            decoded = Image.fromarray(np.asarray(imagecodecs.jpegxl_decode(encoded))).convert("RGB")
            try:
                preview_path = args.output / f"jxl-{slug}.webp"
                preview_output = BytesIO()
                decoded.save(preview_output, format="WEBP", lossless=True, method=6)
                preview_path.write_bytes(preview_output.getvalue())
                entries.append({
                    "id": f"builtin-jxl-{slug}",
                    "name": name,
                    "quality": quality,
                    "effort": 7,
                    "codec": "jpeg-xl",
                    "reference": {"filename": args.reference.name, "size_bytes": len(reference_bytes), "url": "/assets/compression-preview/reference.jpg"},
                    "compressed": {"filename": f"{args.reference.stem}.{slug}.jxl", "size_bytes": len(encoded), "url": f"/assets/compression-preview/jxl-{slug}.webp"},
                    "metrics": {
                        "reference_bytes": len(reference_bytes),
                        "compressed_bytes": len(encoded),
                        "compression_ratio": round(len(reference_bytes) / len(encoded), 3),
                        "compressed_percent": round(len(encoded) / len(reference_bytes) * 100, 2),
                        "mse": round(_mse(reference, decoded), 6),
                    },
                })
            finally:
                decoded.close()
        manifest = {"version": "compression-preview-v1", "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(), "profiles": entries}
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        reference.close()


def _mse(left: Image.Image, right: Image.Image) -> float:
    difference = ImageChops.difference(left, right)
    try:
        width, height = difference.size
        return sum(ImageStat.Stat(difference).sum2) / (width * height * 3)
    finally:
        difference.close()


if __name__ == "__main__":
    main()

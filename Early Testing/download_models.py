"""Download only the declared public weights, with a 5 GB total budget."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=["mobile", "dino", "clip"], default=["mobile", "dino", "clip"])
    args = parser.parse_args()
    urls = {
        "mobile": [("mobile.safetensors", "https://huggingface.co/timm/mobilenetv3_large_100.ra_in1k/resolve/main/model.safetensors")],
        "dino": [("dino.safetensors", "https://huggingface.co/timm/vit_small_patch14_dinov2.lvd142m/resolve/main/model.safetensors")],
        "clip": [("ViT-B-32.pt", "https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt"),
                 ("sa_0_4_vit_b_32_linear.pth", "https://raw.githubusercontent.com/LAION-AI/aesthetic-predictor/main/sa_0_4_vit_b_32_linear.pth")],
    }
    expected_hashes = {
        "mobile.safetensors": "f425af34cc1cead2b5d6211f789a1f30b94835dc32f9c0fcc5a916e4fd2dde85",
        "dino.safetensors": "04d27f3400d059fc0cfd7d17dd1909a75bf3ea8fb3eeb48b97cb99e57ee20081",
        "ViT-B-32.pt": "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af",
        "sa_0_4_vit_b_32_linear.pth": "c7b14cead230694acc7b9447974d3cad78003c72da032e402a303b6c2429e85f",
    }
    folder = ROOT / "models"
    folder.mkdir(exist_ok=True)
    report = []
    for model in args.models:
        for name, url in urls[model]:
            target = folder / name
            downloaded = False
            tick = time.perf_counter()
            if not target.exists():
                print(f"Downloading {name}", flush=True)
                used = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
                partial = target.with_name(target.name + f".{time.time_ns()}.part")
                with urlopen(url, timeout=60) as response, partial.open("xb") as f:
                    while chunk := response.read(1024 * 1024):
                        used += len(chunk)
                        if used > 5_000_000_000:
                            raise ValueError("Model storage budget exceeded")
                        f.write(chunk)
                downloaded = True
            checked = partial if downloaded else target
            with checked.open("rb") as f:
                sha = hashlib.file_digest(f, "sha256").hexdigest()
            if sha != expected_hashes[name]:
                raise ValueError(f"Checksum mismatch: {checked}. No model was loaded.")
            if downloaded:
                partial.rename(target)
            report.append({"model": model, "file": name, "url": url, "size": target.stat().st_size,
                           "sha256": sha, "seconds": time.perf_counter() - tick})
            print(report[-1], flush=True)
    output = folder / f"downloads-{time.time_ns()}.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")

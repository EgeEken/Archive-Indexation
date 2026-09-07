"""Local photo indexing and selection. Archive files are opened read-only."""
import argparse
import hashlib
import json
import os
import math
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parent
ARCHIVE = Path(os.environ["PHOTO_ARCHIVE_ROOT"]).resolve() if os.environ.get("PHOTO_ARCHIVE_ROOT") else None
VERSION = "photo-select-v1"
JPEG = {".jpg", ".jpeg"}
MODEL_NAMES = {"mobile": "mobilenetv3_large_100.ra_in1k",
               "dino": "vit_small_patch14_dinov2.lvd142m"}


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def safe_output(path, source=None):
    path = Path(path).resolve()
    protected = [ARCHIVE] if ARCHIVE is not None else []
    if source is not None:
        source = Path(source).resolve()
        protected.append(source.parent if source.name.casefold() == "all-jpgs" else source)
    if any(path == p or path.is_relative_to(p) for p in protected):
        raise ValueError(f"Output must be outside the photo source/archive: {path}")
    return path


def cache_directory(path, source=None):
    path = safe_output(path, source)
    marker = path / ".photo-select-cache"
    if path.exists() and not marker.exists() and any(path.iterdir()):
        raise ValueError(f"Refusing to use a nonempty unowned cache: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(VERSION, encoding="utf-8")
    elif marker.read_text(encoding="utf-8") != VERSION:
        raise ValueError("Incompatible cache version; use a new cache directory")
    return path


def unit(x):
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-8)


def percentile(x):
    x = np.asarray(x)
    return (rankdata(x, method="average") - 0.5) / max(len(x), 1)


def image_features(path):
    stat = path.stat()
    with Image.open(path) as original:
        width, height = original.size
        exif = original.getexif()
        detail = exif.get_ifd(34665) if 34665 in exif else {}
        timestamp = detail.get(36867) or exif.get(306)
        capture_time, time_source = None, "missing"
        if timestamp:
            try:
                parsed = datetime.strptime(str(timestamp), "%Y:%m:%d %H:%M:%S")
                offset = detail.get(36881)
                if offset:
                    parsed = datetime.fromisoformat(parsed.isoformat() + str(offset))
                    time_source = "exif_with_offset"
                else:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                    time_source = "exif_local_clock"
                capture_time = parsed.timestamp()
                sub = str(detail.get(37521, "0"))
                if sub.isdigit():
                    capture_time += float("0." + sub)
            except (ValueError, TypeError, OverflowError):
                pass
        original.draft("RGB", (1024, 1024))
        im = ImageOps.exif_transpose(original).convert("RGB")
        im.thumbnail((768, 768), Image.Resampling.LANCZOS)
    rgb = np.asarray(im)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0.7)
    lap = cv2.Laplacian(blurred, cv2.CV_32F)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1)
    gradient = gx * gx + gy * gy
    patch_focus, patch_gradient = [], []
    for ys in np.array_split(np.arange(gray.shape[0]), 4):
        for xs in np.array_split(np.arange(gray.shape[1]), 4):
            block = np.ix_(ys, xs)
            variance = float(gray[block].var())
            patch_focus.append(float(lap[block].var()) / (variance + 100.0))
            patch_gradient.append(float(gradient[block].mean()))
    noise = float(np.median(np.abs(gray - cv2.GaussianBlur(gray, (3, 3), 0))))
    hist = np.bincount(gray.astype(np.uint8).ravel(), minlength=256).astype(float)
    hist /= hist.sum()
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    color = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
    color = np.sqrt(color / max(color.sum(), 1))
    spatial = cv2.resize(rgb, (8, 8), interpolation=cv2.INTER_AREA).astype(np.float32).ravel() / 255
    dct = cv2.dct(cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA))[:8, :8].ravel()[1:]
    phash = dct > np.median(dct)
    descriptor = unit(np.concatenate([0.7 * unit(color), 0.5 * unit(spatial),
                                      0.3 * unit(dct)]).astype(np.float32))
    values = {
        "laplacian": float(cv2.Laplacian(gray, cv2.CV_32F).var()),
        "focus": float(np.percentile(patch_focus, 80)),
        "focus_center": float(np.mean(np.array(patch_focus).reshape(4, 4)[1:3, 1:3])),
        "detail": float(np.percentile(patch_gradient, 80)),
        "noise": noise,
        "contrast": float(np.percentile(gray, 95) - np.percentile(gray, 5)),
        "entropy": float(-(hist[hist > 0] * np.log2(hist[hist > 0])).sum()),
        "black_fraction": float((gray < 5).mean()),
        "white_fraction": float((gray > 250).mean()),
        "mean_brightness": float(gray.mean()),
    }
    record = {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
              "width": width, "height": height, "capture_time": capture_time,
              "time_source": time_source, "camera": str(exif.get(272, "")),
              "iso": float(detail.get(34855, 0)), "quality_features": values,
              "phash": "".join("1" if b else "0" for b in phash)}
    return record, descriptor, im


def load_models(names, model_dir, device):
    import torch
    import timm

    model_dir.mkdir(parents=True, exist_ok=True)
    torch.hub.set_dir(str(model_dir / "torch"))
    torch.set_num_threads(4)
    models = {}
    for name in names:
        started = time.perf_counter()
        print(f"Loading {name} on {device}", flush=True)
        if name == "clip":
            import clip
            clip_path = model_dir / "ViT-B-32.pt"
            if not clip_path.exists():
                raise FileNotFoundError("Run python download_models.py first")
            model, transform = clip.load(str(clip_path), device=device)
            head_path = model_dir / "sa_0_4_vit_b_32_linear.pth"
            head = torch.nn.Linear(512, 1)
            head.load_state_dict(torch.load(head_path, map_location="cpu", weights_only=True))
            models[name] = (model.eval(), transform, head.to(device).eval())
        else:
            kwargs = {"img_size": 224} if name == "dino" else {}
            weights = model_dir / (name + ".safetensors")
            if not weights.exists():
                raise FileNotFoundError("Run python download_models.py first")
            model = timm.create_model(MODEL_NAMES[name], pretrained=True, num_classes=0,
                                     pretrained_cfg_overlay={"file": str(weights)}, **kwargs)
            cfg = timm.data.resolve_model_data_config(model)
            cfg["input_size"] = (3, 224, 224)
            transform = timm.data.create_transform(**cfg, is_training=False)
            models[name] = (model.to(device).eval(), transform, None)
        print(f"Loaded {name}: {time.perf_counter() - started:.1f}s", flush=True)
    return models


def index_folder(source, cache, models=None, device="cpu", workers=6, batch_size=32):
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError(f"Source is not a directory: {source}")
    cache = cache_directory(cache, source)
    paths = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in JPEG)
    if any(p.is_symlink() or not p.resolve().is_relative_to(source) for p in paths):
        raise ValueError("Source contains linked files outside its directory")
    if not paths:
        raise ValueError(f"No JPEG images in {source}")
    fingerprint = hashlib.sha256(json.dumps([(str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                            for p in paths]).encode()).hexdigest()
    model_keys = sorted(models or [])
    for saved in cache.glob("*/index.json"):
        candidate = json.loads(saved.read_text(encoding="utf-8"))
        if candidate["fingerprint"] == fingerprint and set(model_keys).issubset(candidate["models"]):
            with np.load(saved.parent / "features.npz", allow_pickle=False) as data:
                return candidate, {k: data[k] for k in data.files}, saved.parent
    key = hashlib.sha256((VERSION + fingerprint + str(model_keys)).encode()).hexdigest()[:24]
    folder = cache / key
    if (folder / "index.json").exists():
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        with np.load(folder / "features.npz", allow_pickle=False) as data:
            arrays = {k: data[k] for k in data.files}
        return index, arrays, folder
    if model_keys and not isinstance(models, dict):
        import torch
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        models = load_models(model_keys, ROOT / "models", device)
    if folder.exists():
        folder = cache / (key + "-" + str(time.time_ns()))
    folder.mkdir()
    thumbs = folder / "thumbs"
    thumbs.mkdir(exist_ok=True)
    records, errors, vectors = [], [], {k: [] for k in ["handcrafted"] + model_keys}
    timings = {"decode_quality_thumbnails_seconds": 0.0, **{k + "_seconds": 0.0 for k in model_keys}}
    started = time.perf_counter()
    cv2.setNumThreads(1)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for start in range(0, len(paths), batch_size):
            tick = time.perf_counter()
            futures = [executor.submit(image_features, p) for p in paths[start:start + batch_size]]
            images, batch_records = [], []
            for p, future in zip(paths[start:start + batch_size], futures):
                try:
                    record, descriptor, im = future.result()
                except Exception as exc:
                    errors.append({"path": str(p), "error": f"{type(exc).__name__}: {exc}"})
                    continue
                thumb_id = len(records) + len(batch_records)
                thumb_path = thumbs / f"{thumb_id:06d}.jpg"
                small = im.copy()
                small.thumbnail((360, 360))
                with thumb_path.open("xb") as f:
                    small.save(f, format="JPEG", quality=78)
                record["thumbnail"] = str(thumb_path)
                batch_records.append(record)
                vectors["handcrafted"].append(descriptor)
                images.append(im)
            timings["decode_quality_thumbnails_seconds"] += time.perf_counter() - tick
            if images and models:
                import torch
                for name, (model, transform, head) in models.items():
                    tick = time.perf_counter()
                    tensor = torch.stack([transform(im) for im in images]).to(device)
                    with torch.inference_mode(), torch.autocast(device_type=device.split(":")[0],
                                                               enabled=device.startswith("cuda")):
                        output = model.encode_image(tensor) if name == "clip" else model(tensor)
                        output = torch.nn.functional.normalize(output.float(), dim=-1)
                        if head is not None:
                            scores = head(output).flatten().float().cpu().numpy()
                            for record, score in zip(batch_records, scores):
                                record["aesthetic"] = float(score)
                    vectors[name].extend(output.float().cpu().numpy())
                    timings[name + "_seconds"] += time.perf_counter() - tick
            records.extend(batch_records)
            print(f"{source.parent.name}: {min(start + batch_size, len(paths))}/{len(paths)} "
                  f"({time.perf_counter() - started:.1f}s)", flush=True)
    if not records:
        raise ValueError(f"All images failed to decode: {errors[:3]}")
    timings["total_seconds"] = time.perf_counter() - started
    index = {"version": VERSION, "source": str(source), "fingerprint": fingerprint,
             "models": model_keys, "device": device, "workers": workers, "batch_size": batch_size,
             "timings": timings, "records": records, "errors": errors}
    arrays = {k: np.asarray(v, dtype=np.float32) for k, v in vectors.items()}
    with (folder / "features.npz").open("xb") as f:
        np.savez_compressed(f, **arrays)
    write_json(folder / "index.json", index)
    return index, arrays, folder


def quality_components(records):
    features = [r["quality_features"] for r in records]
    focus = 0.7 * percentile([f["focus"] for f in features]) + 0.3 * percentile([f["focus_center"] for f in features])
    detail = percentile([f["detail"] for f in features])
    contrast = percentile([f["contrast"] for f in features])
    clipping = np.array([0.35 * f["black_fraction"] + f["white_fraction"] for f in features])
    technical = np.clip(0.65 * focus + 0.20 * detail + 0.15 * contrast - 0.25 * clipping, 0, 1)
    return {"focus": focus, "detail": detail, "contrast": contrast,
            "exposure": np.clip(1 - clipping / 1.35, 0, 1),
            "focus_contribution": 0.65 * focus, "detail_contribution": 0.20 * detail,
            "contrast_contribution": 0.15 * contrast, "clipping_penalty": 0.25 * clipping,
            "technical": technical}


def quality_scores(records, aesthetic_weight=0.0):
    technical = quality_components(records)["technical"]
    if aesthetic_weight:
        if any("aesthetic" not in r for r in records):
            raise ValueError("Aesthetic ranking needs a CLIP index")
        return (1 - aesthetic_weight) * technical + aesthetic_weight * percentile([r["aesthetic"] for r in records])
    return technical


def groups_for(records, embeddings, threshold=0.30, time_weight=0.08):
    n = len(records)
    similarity = np.clip(unit(embeddings) @ unit(embeddings).T, -1, 1)
    if n < 2:
        return np.zeros(n, dtype=int), similarity
    distance = 1 - similarity
    times = np.array([r["capture_time"] if r["capture_time"] is not None else np.nan for r in records])
    delta = np.abs(times[:, None] - times[None, :])
    time_penalty = np.nan_to_num(np.minimum(np.log1p(delta / 30) / np.log1p(3600 / 30), 1), nan=0)
    distance = np.maximum(distance + time_weight * time_penalty, 0)
    np.fill_diagonal(distance, 0)
    tree = linkage(squareform(distance, checks=False), method="complete")
    return fcluster(tree, threshold, criterion="distance") - 1, similarity


def select_indices(records, arrays, count, method="dino", aesthetic_weight=0.0,
                   threshold=None, diversity=0.55, seed=42, trace=None):
    n = len(records)
    if not 0 <= count <= n:
        raise ValueError(f"Count must be between 0 and {n}")
    quality = quality_scores(records, aesthetic_weight)
    groups = np.arange(n)
    similarity = None
    if method in ("mmr", "coverage"):
        from selection_methods import context, select
        prepared = context(records, arrays)
        chosen, quality, details = select(prepared, count, {"strategy": method,
                                          "aesthetic_weight": aesthetic_weight, "diversity": diversity},
                                          explain=trace is not None)
        if trace is not None:
            trace.update(details)
        return chosen, quality, prepared["groups"]
    if method == "random":
        chosen = np.random.default_rng(seed).choice(n, count, replace=False).tolist()
    elif method == "uniform":
        order = sorted(range(n), key=lambda i: (records[i]["capture_time"] is None,
                                               records[i]["capture_time"] or 0, records[i]["path"]))
        chosen = [order[int(i)] for i in np.linspace(0, n - 1, count)] if count else []
    elif method in ("quality", "laplacian", "aesthetic"):
        scores = quality
        if method == "laplacian":
            scores = np.array([r["quality_features"]["laplacian"] for r in records])
        elif method == "aesthetic":
            scores = np.array([r["aesthetic"] for r in records])
        chosen = np.argsort(-scores, kind="stable")[:count].tolist()
    else:
        if method == "time":
            order = sorted(range(n), key=lambda i: (records[i]["capture_time"] is None,
                                                   records[i]["capture_time"] or 0, records[i]["path"]))
            group = 0
            for j, i in enumerate(order):
                prev = records[order[j - 1]]["capture_time"] if j else None
                now = records[i]["capture_time"]
                if j and (prev is None or now is None or now - prev > 30):
                    group += 1
                groups[i] = group
        else:
            if method not in arrays:
                raise ValueError(f"Index does not contain {method} embeddings")
            default_thresholds = {"dino": 0.30, "clip": 0.17, "mobile": 0.30, "handcrafted": 0.12}
            groups, similarity = groups_for(records, arrays[method],
                                            threshold if threshold is not None else default_thresholds[method])
        group_counts = np.bincount(groups)
        group_quality = quality.copy()
        for g in np.unique(groups):
            members = np.flatnonzero(groups == g)
            group_quality[members] = 0.65 * quality[members] + 0.35 * percentile(quality[members])
        selected_per_group = np.zeros(len(group_counts), dtype=int)
        chosen, available, nearest = [], np.ones(n, dtype=bool), np.zeros(n)
        for step in range(count + 1):
            coverage = 1 / (1 + selected_per_group[groups]) ** 1.4
            duplicate_penalty = np.zeros(n)
            score = (1 - diversity) * group_quality + diversity * coverage
            if similarity is not None:
                duplicate_penalty = np.clip((nearest - 0.90) / 0.10, 0, 1)
                score -= 0.35 * duplicate_penalty
            if trace is not None:
                for i in np.flatnonzero(available):
                    trace[int(i)] = {"selection_score": float(score[i]), "evaluated_at_pick": step + 1,
                                     "quality_contribution": float((1 - diversity) * group_quality[i]),
                                     "diversity_contribution": float(diversity * coverage[i]),
                                     "duplicate_penalty": float(0.35 * duplicate_penalty[i]),
                                     "group_quality": float(group_quality[i]),
                                     "group_picks_before": int(selected_per_group[groups[i]])}
            if step == count:
                break
            score[~available] = -np.inf
            best = int(np.argmax(score))
            chosen.append(best)
            available[best] = False
            selected_per_group[groups[best]] += 1
            if similarity is not None:
                nearest = np.maximum(nearest, similarity[best])
    return chosen, quality, groups


def make_selection(index, arrays, count, **options):
    tick = time.perf_counter()
    trace = {}
    chosen, quality, groups = select_indices(index["records"], arrays, count, trace=trace, **options)
    components = quality_components(index["records"])
    selected = set(chosen)
    ranks = {i: rank + 1 for rank, i in enumerate(chosen)}
    records = [{**r, "selected": i in selected, "rank": ranks.get(i),
                "quality_score": float(quality[i]), "image_quality_score": float(components["technical"][i]),
                "score_components": {key: float(value[i]) for key, value in components.items()},
                "selection_details": trace.get(i), "group": int(groups[i])}
               for i, r in enumerate(index["records"])]
    return {"version": VERSION, "score_schema": 2, "source": index["source"], "fingerprint": index["fingerprint"],
            "count": count, "options": options, "selection_seconds": time.perf_counter() - tick,
            "index_timings": index["timings"], "errors": index["errors"],
            "selected_paths": [index["records"][i]["path"] for i in chosen], "records": records}


def write_review(selection, output, manual_paths=None, replace=False):
    from review_ui import write_gallery
    write_gallery(selection, output, manual_paths, replace=replace)


def export_selection(manifest_path, destination):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source = Path(manifest["source"]).resolve()
    destination = safe_output(destination, source)
    records = {r["path"]: r for r in manifest["records"]}
    paths = [Path(p) for p in manifest["selected_paths"]]
    names = [p.name.casefold() for p in paths]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate output names; export cancelled")
    for p in paths:
        if p.suffix.lower() not in JPEG or not p.resolve().is_relative_to(source):
            raise ValueError(f"Invalid source path in selection: {p}")
        stat, expected = p.stat(), records[str(p)]
        if stat.st_size != expected["size"] or stat.st_mtime_ns != expected["mtime_ns"]:
            raise ValueError(f"Source changed since indexing: {p}")
    destination.mkdir(parents=True, exist_ok=False)
    for p in paths:
        with p.open("rb") as src, (destination / p.name).open("xb") as dst:
            shutil.copyfileobj(src, dst)
    return len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="Index JPEGs and write JSON + local HTML; no original copies")
    select.add_argument("source", type=Path)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--cache", type=Path, default=ROOT / "cache")
    size = select.add_mutually_exclusive_group()
    size.add_argument("--count", type=int)
    size.add_argument("--ratio", type=float, default=0.10)
    select.add_argument("--method", choices=["dino", "clip", "mobile", "handcrafted", "time", "quality", "laplacian", "aesthetic", "uniform", "random", "mmr", "coverage"], default="mobile")
    select.add_argument("--aesthetic-weight", type=float, default=0.0)
    select.add_argument("--threshold", type=float)
    select.add_argument("--diversity", type=float, default=0.55)
    select.add_argument("--workers", type=int, default=6)
    select.add_argument("--batch-size", type=int, default=32)
    select.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    export = sub.add_parser("export", help="Copy selected originals into a NEW directory, never overwrite")
    export.add_argument("manifest", type=Path)
    export.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        print(f"Copied {export_selection(args.manifest, args.destination)} photos")
        return
    if not 0 < args.ratio <= 1 or not 0 <= args.aesthetic_weight <= 1 or not 0 <= args.diversity <= 1:
        parser.error("ratio must be in (0,1]; aesthetic-weight and diversity in [0,1]")
    if args.workers < 1 or args.batch_size < 1 or (args.threshold is not None and args.threshold <= 0):
        parser.error("workers, batch-size and threshold must be positive")
    if args.count is not None and args.count < 0:
        parser.error("count cannot be negative")
    if args.threshold is not None and not math.isfinite(args.threshold):
        parser.error("threshold must be finite")
    output = safe_output(args.output, args.source)
    if output.suffix.lower() != ".json":
        parser.error("Output filename must end in .json")
    if output.exists() or output.with_suffix(".html").exists():
        parser.error("Output already exists; choose a new output filename")
    cache = cache_directory(args.cache, args.source)
    names = [args.method] if args.method in ["dino", "clip", "mobile"] else []
    if args.method in ("mmr", "coverage"):
        names = ["dino", "clip"]
    if args.aesthetic_weight or args.method == "aesthetic":
        names = sorted(set(names + ["clip"]))
    index, arrays, _ = index_folder(args.source, cache, names, args.device if names else "cpu", args.workers, args.batch_size)
    count = args.count if args.count is not None else max(1, math.ceil(len(index["records"]) * args.ratio))
    selection = make_selection(index, arrays, count, method=args.method, aesthetic_weight=args.aesthetic_weight,
                               threshold=args.threshold, diversity=args.diversity)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, selection)
    write_review(selection, output.with_suffix(".html"))
    print(f"Selected {count}/{len(index['records'])}: {output}")


if __name__ == "__main__":
    main()

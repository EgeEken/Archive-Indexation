"""Official libjxl command-line support for archival JPEG XL outputs."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageOps, ImageStat

from .jpegxl import profile_settings

TOOL_VERSION = "libjxl-cli-0.12.0-contract-2"


def capabilities() -> dict[str, object]:
    from .capabilities import jpegxl_tool_capabilities

    return jpegxl_tool_capabilities()


def available() -> bool:
    value = capabilities()
    return bool(value.get("available") and value.get("metadata_ready"))


def metadata_contract(source: Path) -> dict[str, object]:
    with Image.open(source) as image:
        exif = _raw_exif(image)
        xmp = _raw_xmp(image)
        icc = bytes(image.info.get("icc_profile") or b"")
        return {
            "orientation": int(image.getexif().get(274, 1) or 1),
            "has_exif": bool(exif),
            "has_gps": 34853 in image.getexif(),
            "has_icc": bool(icc),
            "has_xmp": bool(xmp),
            "alpha": "A" in image.getbands(),
            "mode": image.mode,
            "exif_sha256": _sha256(exif) if exif else None,
            "xmp_sha256": _sha256(xmp) if xmp else None,
            "icc_sha256": _sha256(icc) if icc else None,
            "metadata_policy": "standalone-jxl-container",
            "inventory": metadata_inventory(source),
        }


def metadata_replacement_blocker(source: Path) -> str | None:
    inventory = metadata_inventory(source)
    unsupported = inventory["unsupported"]
    if unsupported:
        return str(unsupported[0])
    return None


def metadata_inventory(source: Path) -> dict[str, object]:
    if source.suffix.casefold() in {".jpg", ".jpeg"}:
        with source.open("rb") as stream:
            return _jpeg_metadata_inventory(stream)
    if source.suffix.casefold() == ".png":
        with source.open("rb") as stream:
            return _png_metadata_inventory(stream)
    return {"format": source.suffix.casefold(), "recognized": [], "unsupported": []}


def _jpeg_metadata_inventory(stream) -> dict[str, object]:
    if isinstance(stream, (bytes, bytearray)):
        stream = BytesIO(stream)
    recognized = []
    unsupported = []
    reader = _CountingReader(stream)
    if reader.read(2) != b"\xff\xd8":
        return {"format": "jpeg", "recognized": [], "unsupported": ["Source is not a valid JPEG file."], "bytes_read": reader.count}
    while True:
        prefix = reader.read(1)
        while prefix and prefix != b"\xff":
            prefix = reader.read(1)
        if not prefix:
            break
        marker_bytes = reader.read(1)
        while marker_bytes == b"\xff":
            marker_bytes = reader.read(1)
        if not marker_bytes:
            break
        marker = marker_bytes[0]
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        length_bytes = reader.read(2)
        if len(length_bytes) != 2:
            unsupported.append("JPEG contains a truncated marker segment.")
            break
        length = int.from_bytes(length_bytes, "big")
        if length < 2:
            unsupported.append("JPEG contains a truncated marker segment.")
            break
        payload = reader.read(length - 2)
        if len(payload) != length - 2:
            unsupported.append("JPEG contains a truncated marker segment.")
            break
        if 0xE0 <= marker <= 0xEF:
            if marker == 0xE0 and payload.startswith((b"JFIF\x00", b"JFXX\x00")):
                recognized.append("APP0/JFIF")
            elif marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
                recognized.append("EXIF")
            elif marker == 0xE1 and payload.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                recognized.append("XMP")
            elif marker == 0xE1 and payload.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
                unsupported.append("Source contains Extended XMP that the current JXL replacement path cannot preserve.")
            elif marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
                recognized.append("ICC")
            elif marker == 0xE2:
                _classify_app2(payload, recognized, unsupported)
            elif marker == 0xEE and payload.startswith(b"Adobe"):
                recognized.append("APP14/Adobe")
            elif marker == 0xED:
                unsupported.append("Source contains JPEG APP13 IPTC/Photoshop metadata that the current JXL replacement path cannot preserve.")
            else:
                unsupported.append(f"Source contains unsupported JPEG APP{marker - 0xE0} metadata (signature {_safe_signature(payload)}, {len(payload)} bytes).")
        elif marker == 0xFE:
            unsupported.append("Source contains a JPEG comment that the current JXL replacement path cannot preserve.")
    return {"format": "jpeg", "recognized": sorted(set(recognized)), "unsupported": sorted(set(unsupported)), "bytes_read": reader.count}


def _png_metadata_inventory(stream) -> dict[str, object]:
    if isinstance(stream, (bytes, bytearray)):
        stream = BytesIO(stream)
    signature = b"\x89PNG\r\n\x1a\n"
    recognized = []
    unsupported = []
    reader = _CountingReader(stream)
    if reader.read(len(signature)) != signature:
        return {"format": "png", "recognized": [], "unsupported": ["Source is not a valid PNG file."], "bytes_read": reader.count}
    while True:
        header = reader.read(8)
        if len(header) != 8:
            break
        length = int.from_bytes(header[:4], "big")
        kind = header[4:8]
        if length > 64 * 1024 * 1024:
            unsupported.append("PNG contains a truncated ancillary chunk.")
            break
        if kind == b"IDAT":
            if not _skip(reader, length):
                unsupported.append("PNG contains a truncated image-data chunk.")
                break
            if len(reader.read(4)) != 4:
                unsupported.append("PNG contains a truncated image-data chunk.")
                break
            continue
        payload_size = min(length, 4096)
        payload = reader.read(payload_size)
        if len(payload) != payload_size:
            unsupported.append("PNG contains a truncated ancillary chunk.")
            break
        if length > len(payload) and not _skip(reader, length - len(payload)):
            unsupported.append("PNG contains a truncated ancillary chunk.")
            break
        if len(reader.read(4)) != 4:
            unsupported.append("PNG contains a truncated ancillary chunk.")
            break
        if kind == b"eXIf":
            recognized.append("EXIF")
        elif kind == b"iCCP":
            recognized.append("ICC")
        elif kind == b"iTXt":
            if payload.startswith(b"XML:com.adobe.xmp\x00"):
                recognized.append("XMP")
            else:
                unsupported.append("PNG contains text metadata that the current JXL replacement path cannot preserve.")
        elif kind in {b"tEXt", b"zTXt"}:
            unsupported.append("PNG contains text metadata that the current JXL replacement path cannot preserve.")
        elif kind in {b"sRGB", b"gAMA", b"cHRM"}:
            unsupported.append("PNG contains color metadata that the current JXL replacement path cannot preserve safely.")
        elif kind == b"pHYs":
            unsupported.append("PNG contains physical-pixel metadata that the current JXL replacement path cannot preserve.")
        elif kind[0] & 0x20 and kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}:
            unsupported.append(f"PNG contains unsupported ancillary chunk {kind.decode('latin1', errors='replace')}.")
        if kind == b"IEND":
            break
    return {"format": "png", "recognized": sorted(set(recognized)), "unsupported": sorted(set(unsupported)), "bytes_read": reader.count}


class _CountingReader:
    def __init__(self, stream):
        self.stream = stream
        self.count = 0

    def read(self, size=-1):
        value = self.stream.read(size)
        self.count += len(value)
        return value


def _skip(reader: _CountingReader, length: int) -> bool:
    try:
        reader.stream.seek(length, 1)
        return len(reader.stream.read(0)) == 0
    except (OSError, AttributeError):
        remaining = length
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                return False
            remaining -= len(chunk)
        return True


def _classify_app2(payload: bytes, recognized: list[str], unsupported: list[str]) -> None:
    if payload.startswith(b"MPF\x00"):
        count = _mpf_image_count(payload)
        if count == 1:
            unsupported.append("JPEG contains single-image MPF metadata that the current JXL replacement path cannot preserve.")
        elif count is not None:
            unsupported.append(f"JPEG contains MPF multi-picture data with {count} images; replacement would discard the associated image.")
        else:
            unsupported.append("JPEG contains MPF multi-picture metadata with an unknown image count.")
        return
    if payload.startswith((b"FPXR", b"FPXR\x00")):
        unsupported.append("JPEG contains FlashPix APP2 metadata that the current JXL replacement path cannot preserve.")
        return
    unsupported.append(f"Unsupported JPEG APP2 metadata (signature {_safe_signature(payload)}, {len(payload)} bytes).")


def _mpf_image_count(payload: bytes) -> int | None:
    tiff = payload[4:]
    if len(tiff) < 8 or tiff[:2] not in {b"II", b"MM"}:
        return None
    order = "little" if tiff[:2] == b"II" else "big"
    if int.from_bytes(tiff[2:4], order) != 42:
        return None
    offset = int.from_bytes(tiff[4:8], order)
    if offset + 2 > len(tiff):
        return None
    count = int.from_bytes(tiff[offset:offset + 2], order)
    for index in range(count):
        entry = offset + 2 + index * 12
        if entry + 12 > len(tiff):
            return None
        tag = int.from_bytes(tiff[entry:entry + 2], order)
        if tag == 0xB001:
            value = int.from_bytes(tiff[entry + 8:entry + 12], order)
            return value if value > 0 else None
    return None


def _safe_signature(payload: bytes) -> str:
    value = payload.split(b"\x00", 1)[0][:24]
    printable = bytes(character if 32 <= character < 127 else ord("?") for character in value)
    return f'"{printable.decode("ascii", errors="replace") or "unknown"}"'


def encode(source: Path, output: Path, profile: dict[str, object], temp_dir: Path) -> dict[str, object]:
    tool = capabilities().get("tools", {}).get("cjxl")
    if not tool or not available():
        raise RuntimeError("Metadata-preserving cjxl runtime is unavailable.")
    source_metadata = metadata_contract(source)
    sidecars: list[Path] = []
    try:
        arguments = [str(tool), str(source), str(output), "--container=1", "--lossless_jpeg=0"]
        settings = profile_settings(profile)
        arguments.extend(["--distance", str(settings["distance"]), "--effort", str(settings["effort"])])
        with Image.open(source) as image:
            exif = _raw_exif(image)
            xmp = _raw_xmp(image)
            icc = bytes(image.info.get("icc_profile") or b"")
        for kind, value in (("exif", exif), ("xmp", xmp), ("icc", icc)):
            if not value:
                continue
            suffix = "icc" if kind == "icc" else kind
            sidecar = temp_dir / f".{output.name}.archive-index-{kind}.tmp"
            sidecar.write_bytes(value)
            sidecars.append(sidecar)
            key = "icc_pathname" if kind == "icc" else kind
            arguments.extend(["-x", f"{key}={sidecar}"])
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=3600)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "cjxl failed").strip())
        return {
            "size_bytes": output.stat().st_size,
            "sha256": _file_sha256(output),
            "metadata_contract": source_metadata,
            "encoder_version": str(capabilities().get("version") or TOOL_VERSION),
            "settings": settings,
        }
    finally:
        for sidecar in sidecars:
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass


def validate(source: Path, output: Path, expected: dict[str, object]) -> dict[str, object]:
    tool = capabilities().get("tools", {}).get("djxl")
    if not tool or not output.is_file() or output.stat().st_size <= 0:
        raise ValueError("JPEG XL output is unavailable for validation.")
    with tempfile.TemporaryDirectory(prefix="archive-index-jxl-validate-") as directory:
        root = Path(directory)
        pixels_path = root / "decoded.png"
        result = subprocess.run([str(tool), str(output), str(pixels_path)], capture_output=True, text=True, timeout=3600)
        if result.returncode:
            raise ValueError("JPEG XL output failed independent djxl validation.")
        with Image.open(source) as source_image, Image.open(pixels_path) as decoded:
            expected_image = ImageOps.exif_transpose(source_image).copy()
            try:
                if decoded.size != expected_image.size:
                    raise ValueError("JPEG XL output dimensions did not match the source display dimensions.")
                if len(decoded.getbands()) != len(expected_image.getbands()):
                    raise ValueError("JPEG XL output channel semantics did not match the source.")
                if "A" in expected_image.getbands() and not _alpha_equal(expected_image, decoded):
                    raise ValueError("JPEG XL output alpha did not match the source exactly.")
                if _catastrophic(expected_image, decoded):
                    raise ValueError("JPEG XL output failed catastrophic visual validation.")
                metadata = _validate_metadata(source, output, root, tool)
                encoded = output.read_bytes()
                return {
                    "size_bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "width": decoded.width,
                    "height": decoded.height,
                    "channels": len(decoded.getbands()),
                    "mse": round(_mse(expected_image, decoded), 6),
                    "alpha_exact": "A" not in expected_image.getbands() or _alpha_equal(expected_image, decoded),
                    "metadata_contract": metadata,
                }
            finally:
                expected_image.close()


def _validate_metadata(source: Path, output: Path, root: Path, tool: str) -> dict[str, object]:
    with Image.open(source) as source_image:
        source_exif = _raw_exif(source_image)
        source_xmp = _raw_xmp(source_image)
        source_icc = bytes(source_image.info.get("icc_profile") or b"")
        source_tags = _required_exif(source_image)
        decoded_path = root / "decoded.jpg" if source_image.mode != "RGBA" else root / "decoded.png"
    result = subprocess.run([str(tool), str(output), str(decoded_path)], capture_output=True, text=True, timeout=3600)
    if result.returncode:
        raise ValueError("JPEG XL metadata output could not be decoded independently.")
    with Image.open(decoded_path) as decoded:
        output_icc = bytes(decoded.info.get("icc_profile") or b"")
        if source_icc and (not output_icc or not _icc_semantics_preserved(source, decoded, source_icc, output_icc)):
            raise ValueError("JPEG XL output did not preserve the source ICC color interpretation.")
        expected_tags = dict(source_tags)
        if expected_tags.get(274) not in {None, 1}:
            expected_tags[274] = 1
        if _required_exif(decoded) != expected_tags:
            raise ValueError("JPEG XL output did not preserve required EXIF metadata.")
        output_orientation = int(decoded.getexif().get(274, 1) or 1)
    output_exif = root / "decoded.exif"
    output_xmp = root / "decoded.xmp"
    exif_result = subprocess.run([str(tool), str(output), str(output_exif), "--output_format", "exif"], capture_output=True, text=True, timeout=3600)
    xmp_result = subprocess.run([str(tool), str(output), str(output_xmp), "--output_format", "xmp"], capture_output=True, text=True, timeout=3600)
    if source_exif:
        if exif_result.returncode:
            raise ValueError("JPEG XL output did not preserve the source EXIF payload.")
        output_exif_bytes = output_exif.read_bytes()
        if source_tags.get(274) in {None, 1} and output_exif_bytes != source_exif:
            raise ValueError("JPEG XL output did not preserve the source EXIF payload.")
    if source_xmp and (xmp_result.returncode or output_xmp.read_bytes() != source_xmp):
        raise ValueError("JPEG XL output did not preserve the source XMP payload.")
    return {
        "metadata_policy": "standalone-jxl-container",
        "exif": bool(source_exif),
        "gps": bool(source_tags.get(34853)),
        "xmp": bool(source_xmp),
        "icc": bool(source_icc and output_icc),
        "orientation": output_orientation,
        "orientation_normalized": source_tags.get(274) not in {None, 1},
        "source_retained": False,
        "tool_version": str(capabilities().get("version") or TOOL_VERSION),
    }


def _raw_exif(image: Image.Image) -> bytes:
    for marker, data in getattr(image, "applist", []):
        if marker == "APP1" and data.startswith(b"Exif\x00\x00"):
            return bytes(data[6:])
    value = image.info.get("exif")
    if value:
        value = bytes(value)
        return value[6:] if value.startswith(b"Exif\x00\x00") else value
    return b""


def _raw_xmp(image: Image.Image) -> bytes:
    value = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
    return bytes(value) if value else b""


def _required_exif(image: Image.Image) -> dict[str, object]:
    exif = image.getexif()
    values = {tag: exif.get(tag) for tag in (271, 272, 306, 36867, 36868, 274, 34853)}
    gps = values.get(34853)
    if isinstance(gps, dict):
        values[34853] = repr(sorted(gps.items()))
    return values


def _icc_semantics_preserved(source: Path, decoded: Image.Image, source_icc: bytes, output_icc: bytes) -> bool:
    try:
        with Image.open(source) as source_image:
            source_display = ImageOps.exif_transpose(source_image).convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
            source_display.info["icc_profile"] = source_icc
            output_display = decoded.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
            output_display.info["icc_profile"] = output_icc
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            source_converted = ImageCms.profileToProfile(source_display, ImageCms.ImageCmsProfile(BytesIO(source_icc)), srgb, outputMode="RGB")
            output_converted = ImageCms.profileToProfile(output_display, ImageCms.ImageCmsProfile(BytesIO(output_icc)), srgb, outputMode="RGB")
            difference = ImageChops.difference(source_converted, output_converted)
            try:
                mean = sum(ImageStat.Stat(difference).mean) / 3
                return mean <= 32.0
            finally:
                difference.close()
                source_converted.close()
                output_converted.close()
                source_display.close()
                output_display.close()
    except Exception:
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _alpha_equal(left: Image.Image, right: Image.Image) -> bool:
    difference = ImageChops.difference(left.getchannel("A"), right.getchannel("A"))
    try:
        return difference.getbbox() is None
    finally:
        difference.close()


def _mse(left: Image.Image, right: Image.Image) -> float:
    left_rgb = left.convert("RGB")
    right_rgb = right.convert("RGB")
    difference = ImageChops.difference(left_rgb, right_rgb)
    try:
        width, height = difference.size
        return sum(ImageStat.Stat(difference).sum2) / (width * height * 3)
    finally:
        difference.close()
        left_rgb.close()
        right_rgb.close()


def _catastrophic(expected: Image.Image, actual: Image.Image) -> bool:
    expected_small = expected.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
    actual_small = actual.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
    try:
        expected_mean = sum(ImageStat.Stat(expected_small).mean) / 3
        actual_mean = sum(ImageStat.Stat(actual_small).mean) / 3
        expected_extrema = expected_small.getextrema()
        actual_extrema = actual_small.getextrema()
        expected_range = max(high for _, high in expected_extrema) - min(low for low, _ in expected_extrema)
        actual_range = max(high for _, high in actual_extrema) - min(low for low, _ in actual_extrema)
        return (expected_mean > 12 and actual_mean < expected_mean * 0.03) or (expected_range > 48 and actual_range < 3)
    finally:
        expected_small.close()
        actual_small.close()

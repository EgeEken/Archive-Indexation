"""Install the pinned official libjxl Windows tools into the app-managed directory."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.12.0"
URL = f"https://github.com/libjxl/libjxl/releases/download/v{VERSION}/jxl-x64-windows-static.zip"
SHA256 = "3025d7e308390796d20492322e606bc92decaee7b6bc99d3f7547870ae5db7de"


def main() -> int:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise SystemExit("LOCALAPPDATA is required on Windows.")
    destination = Path(local_app_data) / "Archive Indexation" / "codecs"
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise SystemExit("The downloaded libjxl archive did not match the pinned SHA-256.")
    temporary = destination.with_name(f"{destination.name}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        for member in package.infolist():
            if member.is_dir() or Path(member.filename).name.casefold() not in {"cjxl.exe", "djxl.exe", "jxlinfo.exe"}:
                continue
            target = temporary / Path(member.filename).name
            target.write_bytes(package.read(member))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    temporary.replace(destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build a wheel and verify every packaged browser resource is present."""

from __future__ import annotations

import ast
import importlib.resources
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def _ui_resources(repository: Path) -> set[str]:
    tree = ast.parse((repository / "src" / "archive_index" / "api" / "server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_UI_RESOURCES" for target in node.targets):
            return set(ast.literal_eval(node.value))
    raise RuntimeError("server.py does not define _UI_RESOURCES")


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    resources = _ui_resources(repository)
    with tempfile.TemporaryDirectory(prefix="archive-index-package-") as directory:
        root = Path(directory)
        wheels = root / "wheels"
        target = root / "target"
        wheels.mkdir()
        target.mkdir()
        environment = os.environ.copy()
        environment["PIP_CACHE_DIR"] = str(root / "pip-cache")
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(repository), "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels)],
            check=True,
            env=environment,
        )
        wheel = next(wheels.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
        missing_from_wheel = {f"archive_index/web/{resource}" for resource in resources if f"archive_index/web/{resource}" not in names}
        if missing_from_wheel:
            raise AssertionError(f"wheel is missing web resources: {sorted(missing_from_wheel)}")
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)], check=True, env=environment)
        code = """
import importlib.resources
import sys
from pathlib import Path
target, resources = Path(sys.argv[1]), set(sys.argv[2:])
sys.path.insert(0, str(target))
package = importlib.resources.files('archive_index.web')
missing = [resource for resource in resources if not package.joinpath(resource).is_file()]
if missing:
    raise SystemExit(f'missing installed resources: {sorted(missing)}')
print(f'installed web resources verified: {len(resources)}')
"""
        subprocess.run([sys.executable, "-c", code, str(target), *sorted(resources)], check=True)
        print(f"wheel verified: {wheel.name}; resources: {len(resources)}")


if __name__ == "__main__":
    main()

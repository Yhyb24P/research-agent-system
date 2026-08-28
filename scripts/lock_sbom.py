"""Render a dependency-lock-derived CycloneDX SBOM without network access."""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any


def build(lock_path: Path) -> dict[str, Any]:
    with lock_path.open("rb") as stream:
        lock = tomllib.load(stream)
    components: list[dict[str, Any]] = []
    for package in lock.get("package", []):
        if not isinstance(package, dict) or not package.get("name") or not package.get("version"):
            continue
        component: dict[str, Any] = {
            "type": "library",
            "name": package["name"],
            "version": package["version"],
            "purl": f"pkg:pypi/{package['name']}@{package['version']}",
        }
        wheels = package.get("wheels", [])
        if isinstance(wheels, list):
            hashes = sorted(
                item["hash"] for item in wheels
                if isinstance(item, dict) and isinstance(item.get("hash"), str)
            )
            if hashes:
                component["hashes"] = [{"alg": "SHA-256", "content": value.removeprefix("sha256:")} for value in hashes]
        components.append(component)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"tools": [{"vendor": "research-agent-system", "name": "lock_sbom", "version": "1"}]},
        "components": sorted(components, key=lambda item: (item["name"], item["version"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sbom = build(args.lock)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "components": len(sbom["components"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

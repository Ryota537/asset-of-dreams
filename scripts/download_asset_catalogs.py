"""Download and decompress the Unity Addressables asset catalogs."""

import sys
import urllib.error
import urllib.request
from pathlib import Path

import brotli

current_dir = Path(__file__).resolve().parent
instance_dir = current_dir.parent
project_root = instance_dir.parent
for p in [str(project_root), str(instance_dir), str(current_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts._sirius import MaintenanceError, environment  # noqa: E402

OUT = project_root / "_data" / "assets"
ASSET_URL = "https://assets-e.wds-stellarium.com/production"
KINDS = ("2d-assets", "3d-assets", "cri-assets")
PLATFORMS = ("Android", "iOS")


def catalog_url(kind: str, platform: str, version: str) -> str:
    return f"{ASSET_URL}/{kind}/{platform}/{version}/catalog_{version}.json.br"


def get_catalog_version() -> str:
    return str(environment().asset_version)


def download_catalogs(out_dir: Path = OUT) -> str:
    version = get_catalog_version()
    print(f"asset version {version}")
    ok = 0
    for kind in KINDS:
        for platform in PLATFORMS:
            url = catalog_url(kind, platform, version)
            print(f"downloading {url}")
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "server-of-dreams"}
                )
                data = urllib.request.urlopen(req, timeout=120).read()
            except urllib.error.HTTPError as e:
                print(f"  skipped ({e.code} {e.reason})")
                continue
            catalog = brotli.decompress(data)
            dest = out_dir / kind / platform.lower() / "catalog.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(catalog)
            print(
                f"  {len(data)} br -> {len(catalog)} json -> {dest}"
            )
            ok += 1
    print(f"wrote {ok} catalogs -> {out_dir}")
    return version


def main() -> None:
    try:
        download_catalogs()
    except MaintenanceError:
        print("Server is in maintenance")


if __name__ == "__main__":
    main()

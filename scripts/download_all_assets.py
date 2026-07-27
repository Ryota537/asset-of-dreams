"""Download every asset bundle for the game into ``_data/assets/`` (Android + iOS)."""

import argparse
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

current_dir = Path(__file__).resolve().parent
instance_dir = current_dir.parent
project_root = instance_dir.parent
for p in [str(project_root), str(instance_dir), str(current_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts._sirius import MaintenanceError, environment  # noqa: E402

ASSETS = project_root / "_data" / "assets"
ASSET_URL = "https://assets-e.wds-stellarium.com/production"
KINDS = ("2d-assets", "3d-assets", "cri-assets")
PLATFORMS = ("Android", "iOS")
_REF = re.compile(r"^(\d+)#(.*)$")


def bundle_rel_paths(catalog_path: Path) -> list:
    """Distinct ``<group>/<name>.bundle`` paths (relative to the version dir)."""
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    prefixes = data["m_InternalIdPrefixes"]
    out, seen = [], set()
    for iid in data["m_InternalIds"]:
        if not iid.endswith(".bundle"):
            continue
        m = _REF.match(iid)
        resolved = (
            prefixes[int(m.group(1))] + m.group(2)
            if m and int(m.group(1)) < len(prefixes)
            else iid
        )
        tail = resolved.split("://", 1)[-1].split("/", 2)
        rel = tail[2] if len(tail) == 3 else resolved
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def _download(url: str, dest: Path) -> str:
    if dest.exists():
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "server-of-dreams"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)
    return "ok"


def download_assets_for_kind_platform(
    kind: str,
    platform: str,
    assets_dir: Path = ASSETS,
    workers: int = 16,
    limit: int = 0,
    version: str = None,
) -> int:
    if version is None:
        version = str(environment().asset_version)
    root = assets_dir / kind / platform.lower()
    catalog = root / "catalog.json"
    if not catalog.is_file():
        print(f"{kind}/{platform}: no catalog (run download_asset_catalogs)")
        return 0
    rels = bundle_rel_paths(catalog)
    if limit:
        rels = rels[:limit]
    print(f"Downloading {kind}/{platform}: {len(rels)} bundles")

    ok = skip = err = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _download,
                f"{ASSET_URL}/{kind}/{platform}/{version}/{rel}",
                root / rel,
            ): rel
            for rel in rels
        }
        for fut in as_completed(futures):
            done += 1
            try:
                res = fut.result()
                ok += res == "ok"
                skip += res == "skip"
            except urllib.error.HTTPError as e:
                err += 1
                print(f"  ! {futures[fut]} ({e.code})")
            except Exception as e:  # noqa: BLE001
                err += 1
                print(f"  ! {futures[fut]} ({type(e).__name__})")
            if done % 200 == 0 or done == len(rels):
                print(f"  {done}/{len(rels)} ok={ok} skip={skip} err={err}")
    return len(rels)


def download_all_assets_func(assets_dir: Path = ASSETS, workers: int = 16, limit: int = 0) -> int:
    version = str(environment().asset_version)
    grand_total = 0
    for kind in KINDS:
        for platform in PLATFORMS:
            grand_total += download_assets_for_kind_platform(
                kind, platform, assets_dir=assets_dir, workers=workers, limit=limit, version=version
            )

    print(f"total bundles processed: {grand_total}")
    return grand_total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="count bundles only")
    parser.add_argument("--workers", type=int, default=16, help="parallel downloads")
    parser.add_argument("--limit", type=int, default=0, help="max bundles per catalog")
    args = parser.parse_args()

    if args.dry_run:
        print("Dry run mode")
        return

    try:
        download_all_assets_func(workers=args.workers, limit=args.limit)
    except MaintenanceError:
        print("Server is in maintenance")


if __name__ == "__main__":
    main()

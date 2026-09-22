"""Build an index of every sprite name inside the sprite-atlas bundles.

    python -m scripts.download_atlas_index
    python -m scripts.download_atlas_index --output _data/asset_index/atlas_index.json

``download_static_assets --skip-atlas`` needs this index to tell a JewelShop icon that is a
real loose file from one that is only a sprite inside an atlas bundle. Without it that step
warns and processes every path, and the atlas-backed values then 404.

Why an index and not a downloader: the atlases are Addressables bundles, so they belong to
the ``2d-assets`` set that ``download_all_assets`` already fetches. This script only reads
the atlas bundle list from the local catalog, fetches those 31 bundles into a scratch
directory, and records the sprite names -- a few hundred MB that is deleted afterwards.

``strings`` cannot do this: UnityFS is compressed, so names come out truncated
(``2bai_comb`` instead of ``2bai_combo``) and sprites look absent.

Needs UnityPy, which is intentionally NOT in requirements.txt (the release pipeline does not
need it). Install it into the venv that runs this script: ``pip install UnityPy``.
"""

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
DEFAULT_OUT = project_root / "_data" / "asset_index" / "atlas_index.json"
SCRATCH = project_root / "_data" / "asset_index" / "atlas_bundles"
_REF = re.compile(r"^(\d+)#(.*)$")
_UA = "server-of-dreams"


def atlas_bundle_paths(kind: str = "2d-assets", platform: str = "android") -> list:
    """``<group>/<name>.bundle`` paths of the sprite-atlas bundles, from the local catalog."""
    catalog = ASSETS / kind / platform / "catalog.json"
    if not catalog.is_file():
        raise FileNotFoundError(
            f"no catalog at {catalog} (run scripts.download_asset_catalogs first)"
        )
    data = json.loads(catalog.read_text(encoding="utf-8"))
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
        # the "spriteatlas" marker lives in the RESOLVED prefix, not in the raw internal id
        # (which is just `947#/accessories.bundle`), so filter after resolving.
        if "spriteatlas" not in resolved.lower():
            continue
        tail = resolved.split("://", 1)[-1].split("/", 2)
        rel = tail[2] if len(tail) == 3 else resolved
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return sorted(out)


def _looks_like_challenge(head: bytes) -> bool:
    """True when the CDN returned an anti-bot challenge page instead of the asset."""
    h = head[:64].lstrip().lower()
    return h.startswith(b"<script") or h.startswith(b"<html") or h.startswith(b"<!doctype")


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read()
    if not body or _looks_like_challenge(body):
        raise RuntimeError("WAF challenge")
    tmp = dest.with_name(dest.name + ".part")
    with open(tmp, "wb") as f:
        f.write(body)
    tmp.replace(dest)


def _sprite_names(bundle: Path) -> list:
    import UnityPy  # imported lazily so --help works without it

    env = UnityPy.load(str(bundle))
    return [
        o.read().m_Name for o in env.objects if o.type.name == "Sprite"
    ]


def build_index(
    version: str = None,  # type: ignore[assignment]
    output: Path = DEFAULT_OUT,
    scratch: Path = SCRATCH,
    workers: int = 3,
    keep_bundles: bool = False,
) -> dict:
    """Download the atlas bundles, index their sprite names, and write ``output``."""
    # fail before downloading hundreds of MB when the parser is missing
    import UnityPy  # noqa: F401

    if version is None:
        version = str(environment().asset_version)
    rels = atlas_bundle_paths()
    print(f"atlas bundles: {len(rels)} (asset version {version})")

    index, err = {}, 0
    try:
        for rel in rels:
            dest = scratch / Path(rel).name
            if not dest.is_file():
                try:
                    _fetch(f"{ASSET_URL}/2d-assets/Android/{version}/{rel}", dest)
                except (urllib.error.HTTPError, RuntimeError, OSError) as e:
                    err += 1
                    print(f"  ! {rel} ({type(e).__name__})")
                    continue
            try:
                names = _sprite_names(dest)
            except Exception as e:  # noqa: BLE001
                err += 1
                print(f"  ! {rel} (parse: {type(e).__name__})")
                continue
            index[Path(rel).name] = sorted(set(names))
            print(f"  {Path(rel).name:44s} {len(names):6d} sprites")
    finally:
        if not keep_bundles and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v) for v in index.values())
    print(f"\n{len(index)} bundles, {total} sprite names, {err} error(s)")
    print(f"-> {output}")
    return index


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT, help="where to write the index")
    parser.add_argument("--version", default=None, help="asset version (default: ask the API)")
    parser.add_argument("--workers", type=int, default=3, help="parallel downloads (WAF: keep low)")
    parser.add_argument("--keep-bundles", action="store_true", help="do not delete the bundles")
    args = parser.parse_args()

    try:
        build_index(
            version=args.version,
            output=args.output,
            workers=args.workers,
            keep_bundles=args.keep_bundles,
        )
    except MaintenanceError:
        print("Server is in maintenance")
    except ModuleNotFoundError as e:
        print(f"missing dependency: {e.name} -- install it with: pip install UnityPy")
    except FileNotFoundError as e:
        print(e)


if __name__ == "__main__":
    main()

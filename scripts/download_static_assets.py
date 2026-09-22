"""Download every loose static-asset the client fetches from ``{staticContentUrl}``.

    python -m scripts.download_static_assets --dry-run
    python -m scripts.download_static_assets --skip-atlas
    python -m scripts.download_static_assets --skip-atlas --astc

``{staticContentUrl}`` is ``<asset_url>/static-assets``. It is NOT an Addressables bundle
store: it serves Unity ``Resources/`` paths, where the **directory comes from a client
string literal** and the **filename comes from a master-data column**. Prefixing a
master-data value straight onto the host gives 404, so every path below is built from the
literal template the client actually uses.

Confirmed layouts (all verified HTTP 200 against the live CDN):

- ``Resources/Textures/Banners/{BannerMaster.image_path}.png``
- ``Resources/Textures/HomePoster/{HomePosterMaster.poster_master_id}.png``
- ``Resources/Textures/Comic/{stem}.png``
- ``Resources/Textures/JewelShop/PickUp/{icon}.png``  (only the loose icons)

The Comic stem is the ``Data`` of the ``Element: 6`` entry inside
``ComicMaster.episodes[].body`` (a stringified rich-text list) -- it is NOT
``ComicMaster.id``, and no column holds it.

Two things that look like failures but are not:

- Most JewelShop icon values name a **sprite inside a sprite atlas bundle**, not a file, so
  they 404 here. ``--skip-atlas`` filters them out with an atlas index.
- Every texture also exists as a parallel **``.astc.gz``** twin (the client literal carries
  no extension and picks between ``.png`` and ``.astc.gz`` at runtime). ``--astc`` fetches
  those too. ``Comic`` has no ASTC variant, so its ``.astc.gz`` 404s are expected.

Requires the master data to be unpacked first (``scripts.download_masterdata``).
Existing files are skipped, so re-running resumes.
"""

import argparse
import json
import re
import sys
import time
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

ASSETS = project_root / "_data" / "assets"
MASTERDATA = project_root / "_data" / "masterdata"
DEFAULT_ATLAS_INDEX = project_root / "_data" / "asset_index" / "atlas_index.json"

ASSET_URL = "https://assets-e.wds-stellarium.com/production"
STATIC_URL = f"{ASSET_URL}/static-assets"
OUT = ASSETS / "static-assets"
_UA = "server-of-dreams"

# the only place a JewelShop icon can live as a loose file
JEWEL_PICKUP_DIR = "Resources/Textures/JewelShop/PickUp/"
JEWEL_PICKUP = JEWEL_PICKUP_DIR + "{}.png"
JEWEL_COLUMNS = (
    "item_icon_body_image_path",
    "item_icon_detail_image_path",
    "item_icon_badge_image_path",
)


def _load(name: str) -> list:
    return json.loads((MASTERDATA / f"{name}.json").read_text(encoding="utf-8"))


def comic_stems() -> set:
    """Comic image stems: the ``Data`` of the ``Element: 6`` entry in each episode body."""
    stems = set()
    for group in _load("ComicMaster"):
        for episode in group.get("episodes") or []:
            body = episode.get("body")
            if not body:
                continue
            try:
                elements = json.loads(body)
            except ValueError:
                # not every body is a well-formed list; fall back to a regex sweep
                stems.update(re.findall(r'"Data"\s*:\s*"([^"]+)"', body))
                continue
            for element in elements:
                if isinstance(element, dict) and element.get("Element") == 6:
                    data = element.get("Data")
                    if data:
                        stems.add(data)
    return stems


def expected_files() -> list:
    """Relative paths under ``static-assets/`` implied by the master data."""
    out = set()

    for row in _load("BannerMaster"):
        value = row.get("image_path")
        if value:
            out.add(f"Resources/Textures/Banners/{value}.png")

    for row in _load("HomePosterMaster"):
        value = row.get("poster_master_id")
        if value:
            out.add(f"Resources/Textures/HomePoster/{value}.png")

    for stem in comic_stems():
        out.add(f"Resources/Textures/Comic/{stem}.png")

    for row in _load("JewelShopItemMaster"):
        for column in JEWEL_COLUMNS:
            value = row.get(column)
            if value:
                out.add(JEWEL_PICKUP.format(value))

    return sorted(out)


def atlas_names(index_path: Path) -> set:
    """Every sprite name indexed across the sprite-atlas bundles (may be empty)."""
    if not index_path.is_file():
        return set()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return {name for names in index.values() for name in names}


def _looks_like_challenge(head: bytes) -> bool:
    """True when the CDN returned an anti-bot challenge page instead of the asset."""
    h = head[:64].lstrip().lower()
    return h.startswith(b"<script") or h.startswith(b"<html") or h.startswith(b"<!doctype")


def _probe(url: str) -> int:
    """Status via a 1-byte ranged GET -- HEAD is unreliable on this host."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _download(url: str, dest: Path, attempts: int = 6) -> str:
    if dest.exists() and not _looks_like_challenge(dest.read_bytes()[:64]):
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            body = _fetch(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            body = b""
        if body and not _looks_like_challenge(body):
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as f:
                f.write(body)
            tmp.replace(dest)  # atomic: a killed download never leaves a "complete" file
            return "ok"
        if attempt == attempts:
            raise RuntimeError("WAF challenge persisted")
        time.sleep(delay)
        delay = min(delay * 2, 20.0)
    return "err"


def _filter_atlas(rels: list, index_path: Path) -> list:
    """Drop JewelShop values that name an atlas sprite instead of a loose file."""
    known = atlas_names(index_path)
    if not known:
        print(f"! atlas index not found at {index_path}; nothing filtered", file=sys.stderr)
        return rels
    # Only JewelShop values are filtered: there the master-data value is the WHOLE filename,
    # so it either is a loose file or is an atlas sprite. Everywhere else the filename is a
    # number (`Banners/Home/1`, `HomePoster/320080`) that collides with unrelated atlas
    # sprite names -- `stagesetting` alone contributes 1..233 -- so a naive stem-vs-atlas
    # filter silently drops real files.
    kept = [
        r for r in rels
        if not (r.startswith(JEWEL_PICKUP_DIR) and Path(r).stem in known)
    ]
    print(f"skipped {len(rels) - len(kept)} atlas-backed values ({len(known)} sprites indexed)")
    return kept


def download_static_assets_func(
    assets_dir: Path = ASSETS,
    workers: int = 3,
    limit: int = 0,
    skip_atlas: bool = False,
    astc: bool = False,
    atlas_index: Path = DEFAULT_ATLAS_INDEX,
) -> int:
    """Download static assets under ``assets_dir``. Returns the number of paths attempted."""
    out = assets_dir / "static-assets"
    rels = expected_files()

    if skip_atlas:
        rels = _filter_atlas(rels, atlas_index)

    if limit:
        rels = rels[:limit]

    # The client picks .png or .astc.gz at runtime (the literal carries no extension), so
    # --astc fetches the twin of each path.
    if astc:
        rels = rels + [r[:-4] + ".astc.gz" for r in rels]
        print(f"  (--astc: {len(rels)} paths including .astc.gz twins)")
    print(f"Downloading static assets: {len(rels)} files -> {out}")

    ok = skip = err = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download, f"{STATIC_URL}/{rel}", out / rel): rel for rel in rels
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="probe every file, download none")
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="parallel requests (keep low: assets-e throttles into a WAF challenge)",
    )
    parser.add_argument("--limit", type=int, default=0, help="max files (debugging)")
    parser.add_argument(
        "--skip-atlas",
        action="store_true",
        help="skip values that name a sprite in a sprite-atlas bundle (they have no file here)",
    )
    parser.add_argument(
        "--atlas-index",
        type=Path,
        default=DEFAULT_ATLAS_INDEX,
        help="atlas index used by --skip-atlas",
    )
    parser.add_argument(
        "--astc",
        action="store_true",
        help=(
            "also fetch the parallel .astc.gz twin of every file (the client literal has no "
            "extension and picks between .png and .astc.gz at runtime). Roughly half the "
            "size of the PNG; Comic has none."
        ),
    )
    args = parser.parse_args()

    if not (MASTERDATA / "BannerMaster.json").is_file():
        print(f"no master data at {MASTERDATA} (run scripts.download_masterdata first)")
        return

    rels = expected_files()
    if args.skip_atlas:
        rels = _filter_atlas(rels, args.atlas_index)
    if args.limit:
        rels = rels[: args.limit]
    if args.astc:
        rels = rels + [r[:-4] + ".astc.gz" for r in rels]
        print(f"  (--astc: {len(rels)} paths including .astc.gz twins)")
    print(f"{len(rels)} static-assets files expected -> {OUT}")

    if args.dry_run:
        missing = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_probe, f"{STATIC_URL}/{rel}"): rel for rel in rels}
            for fut in as_completed(futures):
                if fut.result() not in (200, 206):
                    missing.append((futures[fut], fut.result()))
        print(f"available: {len(rels) - len(missing)}/{len(rels)} | missing: {len(missing)}")
        for rel, code in sorted(missing)[:40]:
            print(f"  ! {rel} ({code})")
        return

    download_static_assets_func(
        workers=args.workers,
        limit=args.limit,
        skip_atlas=args.skip_atlas,
        astc=args.astc,
        atlas_index=args.atlas_index,
    )


if __name__ == "__main__":
    main()

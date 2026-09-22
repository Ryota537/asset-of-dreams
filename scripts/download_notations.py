"""Download every notation chart + music_config into ``_data/assets/Notations/``.

    python -m scripts.download_notations --dry-run
    python -m scripts.download_notations

The CDN layout is ``<asset_url>/Notations/<dir>/<file>`` where:

- ``<dir>`` is ``LiveMaster.music_master_id`` for normal charts, or
  ``AnotherNotationMaster.notation_path`` for alternate charts. A chart's directory is
  NOT ``LiveMaster.id`` -- that id never appears in a URL.
- ``<file>`` is ``<difficulty>.enc`` (the row's difficulty value, not a fixed ``1.enc``)
  and ``music_config.enc`` for the shared per-directory config.

Requires the master data to be unpacked first (``scripts.download_masterdata``).

Files are AES/brotli-encrypted; the client decrypts them, so they are stored verbatim.
Existing files are skipped, so re-running resumes.
"""

import argparse
import json
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
ASSET_URL = "https://assets-e.wds-stellarium.com/production"
OUT = ASSETS / "Notations"
_UA = "server-of-dreams"


def expected_files() -> list:
    """Distinct ``<dir>/<file>`` notation paths implied by the master data."""
    dirs = {}  # dir -> set of chart filenames

    live = json.loads((MASTERDATA / "LiveMaster.json").read_text(encoding="utf-8"))
    for row in live:
        d = str(row["music_master_id"])
        dirs.setdefault(d, set()).add(f"{row['difficulty']}.enc")

    another = json.loads(
        (MASTERDATA / "AnotherNotationMaster.json").read_text(encoding="utf-8")
    )
    for row in another:
        d = str(row["notation_path"])
        dirs.setdefault(d, set()).add(f"{row['difficulty']}.enc")

    out = []
    for d, names in dirs.items():
        for name in sorted(names):
            out.append(f"{d}/{name}")
        out.append(f"{d}/music_config.enc")
    return sorted(out)


def _looks_like_challenge(head: bytes) -> bool:
    """True when the CDN returned an anti-bot challenge page instead of the asset.

    assets-e sits behind a WAF that, under concurrency, answers 200 with an obfuscated
    ``<script>`` HTML page (content-type text/html) instead of the encrypted binary.
    Writing that to disk silently corrupts the asset, so it is detected and retried.
    """
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


def download_notations_func(assets_dir: Path = ASSETS, workers: int = 3, limit: int = 0) -> int:
    """Download all notations under ``assets_dir``. Returns the number of paths attempted."""
    out = assets_dir / "Notations"
    rels = expected_files()
    if limit:
        rels = rels[:limit]
    print(f"Downloading notations: {len(rels)} files -> {out}")

    ok = skip = err = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download, f"{ASSET_URL}/Notations/{rel}", out / rel): rel
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="probe every file, download none")
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="parallel downloads (keep low: assets-e throttles into a WAF challenge)",
    )
    parser.add_argument("--limit", type=int, default=0, help="max files (debugging)")
    args = parser.parse_args()

    if not (MASTERDATA / "LiveMaster.json").is_file():
        print(f"no master data at {MASTERDATA} (run scripts.download_masterdata first)")
        return

    rels = expected_files()
    if args.limit:
        rels = rels[: args.limit]
    print(f"{len(rels)} notation files expected -> {OUT}")

    if args.dry_run:
        missing = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_probe, f"{ASSET_URL}/Notations/{rel}"): rel for rel in rels}
            for fut in as_completed(futures):
                if fut.result() not in (200, 206):
                    missing.append((futures[fut], fut.result()))
        print(f"available: {len(rels) - len(missing)}/{len(rels)} | missing: {len(missing)}")
        for rel, code in sorted(missing)[:40]:
            print(f"  ! {rel} ({code})")
        return

    download_notations_func(workers=args.workers, limit=args.limit)


if __name__ == "__main__":
    main()

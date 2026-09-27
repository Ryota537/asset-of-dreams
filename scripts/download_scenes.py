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
MASTER_URL = "https://raw.githubusercontent.com/wds-sirius/wds-bin-archive/main/episode.json"
ASSET_URL = "https://assets-e.wds-stellarium.com/master-data/production"
OUT = ASSETS / "scenes"
_UA = "server-of-dreams"


def load_episode_master() -> list:
    """Load EpisodeMaster.json from the github."""
    data = json.loads(_fetch(MASTER_URL).decode("utf-8"))
    return data

def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def expected_files() -> list:
    """Distinct ``<dir>/<file>`` notation paths implied by the master data."""

    out = []
    episodes = load_episode_master()
    for id in episodes:
        out.append(f"{episodes[str(id)].get('episodeDetailAssetSource').split('scenes/')[-1]}")

    return sorted(out)

def _looks_like_challenge(head: bytes) -> bool:
    """True when the CDN returned an anti-bot challenge page instead of the asset.

    assets-e sits behind a WAF that, under concurrency, answers 200 with an obfuscated
    ``<script>`` HTML page (content-type text/html) instead of the encrypted binary.
    Writing that to disk silently corrupts the asset, so it is detected and retried.
    """
    h = head[:64].lstrip().lower()
    return h.startswith(b"<script") or h.startswith(b"<html") or h.startswith(b"<!doctype")
    
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

def download_scenes_func(
assets_dir: Path = ASSETS,
workers: int = 3,
limit: int = 0
) -> int:
    
    rels = expected_files()
    if limit:
        rels = rels[:limit]
    ok = skip = err = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download, f"{ASSET_URL}/scenes/{rel}", OUT / rel): rel
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

    download_scenes_func(workers=args.workers, limit=args.limit)
        
if __name__ == "__main__":
    main()
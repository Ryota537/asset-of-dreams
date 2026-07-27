"""GitHub Actions Runner Script for World Dai Star Asset & Masterdata Downloads & Releases."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

current_dir = Path(__file__).resolve().parent
instance_dir = current_dir.parent
project_root = instance_dir.parent
for p in [str(project_root), str(instance_dir), str(current_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Force unbuffered output so logs flush immediately in CI / GitHub Actions
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

from scripts.download_masterdata import (
    get_masterdata_version,
    download_and_unpack_masterdata,
)
from scripts.download_asset_catalogs import (
    get_catalog_version,
    download_catalogs,
)
from scripts.download_all_assets import (
    download_all_assets_func,
    download_assets_for_kind_platform,
)

KINDS = ("2d-assets", "3d-assets", "cri-assets")
PLATFORMS = ("Android", "iOS")


def create_zips_from_dir(source_dir: Path, output_dir: Path, base_name: str) -> list[Path]:
    """Compress all files in source_dir into output_dir, splitting into multiple zips if size > 1.8 GB."""
    output_dir.mkdir(parents=True, exist_ok=True)
    all_files = [f for f in source_dir.rglob("*") if f.is_file()]
    if not all_files:
        return []

    # Sort files deterministically (catalog.json first)
    all_files.sort(key=lambda p: (0 if p.name == "catalog.json" else 1, str(p)))

    MAX_BYTES_PER_ZIP = 1_800_000_000  # 1.8 GB threshold (below GitHub 2 GiB limit)

    batches = []
    current_files = []
    current_size = 0

    for file_path in all_files:
        fsize = file_path.stat().st_size
        if current_files and (current_size + fsize > MAX_BYTES_PER_ZIP):
            batches.append(current_files)
            current_files = [file_path]
            current_size = fsize
        else:
            current_files.append(file_path)
            current_size += fsize

    if current_files:
        batches.append(current_files)

    created_zips = []
    for idx, batch in enumerate(batches, start=1):
        zip_name = f"{base_name}.zip" if len(batches) == 1 else f"{base_name}-{idx}.zip"
        zip_path = output_dir / zip_name
        print(f"Creating zip {zip_path.name} from {source_dir} ({len(batch)} file(s), part {idx}/{len(batches)})...")
        file_count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in batch:
                arcname = file_path.relative_to(source_dir)
                zf.write(file_path, arcname)
                file_count += 1
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        print(f"  compressed {file_count} files -> {zip_path.name} ({size_mb:.2f} MB)")
        created_zips.append(zip_path)

    return created_zips


def fetch_github_releases() -> list[dict]:
    """Fetch existing GitHub releases for repository using GitHub API."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("[GitHub API] GITHUB_REPOSITORY not set. Treating as run with no existing releases.")
        return []

    url = f"https://api.github.com/repos/{repo}/releases"
    headers = {
        "User-Agent": "server-of-dreams-auto-release",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"[GitHub API] Fetching existing releases for repository: {repo}...")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            releases = data if isinstance(data, list) else []
            print(f"[GitHub API] Successfully retrieved {len(releases)} existing release(s).")
            return releases
    except urllib.error.HTTPError as e:
        print(f"[GitHub API Error] ({e.code} {e.reason}) while fetching releases from {url}")
        return []
    except Exception as e:
        print(f"[GitHub API Error] Exception occurred while fetching releases: {e}")
        return []


def analyze_release_status(
    releases: list[dict], catalog_version: str, masterdata_version_safe: str
) -> tuple[str, str]:
    """
    Returns:
      action: 'RELEASE_BOTH' | 'RELEASE_MASTERDATA_ONLY' | 'SKIP'
      release_tag: str
    """
    print("\n--- Analyzing Release Status ---")
    catalog_release = None
    for r in releases:
        if r.get("tag_name") == catalog_version:
            catalog_release = r
            break

    if catalog_release:
        print(f"  Catalog Release Status : FOUND (Tag '{catalog_version}')")
    else:
        print(f"  Catalog Release Status : NOT FOUND (No release matching catalog version '{catalog_version}')")

    masterdata_found = False
    increment_nums = []
    prefix = f"{catalog_version}-"

    for r in releases:
        tag = r.get("tag_name", "")
        if tag == catalog_version or tag.startswith(prefix):
            if tag.startswith(prefix):
                suffix = tag[len(prefix):]
                if suffix.isdigit():
                    increment_nums.append(int(suffix))

            for asset in r.get("assets", []):
                asset_name = asset.get("name", "")
                if masterdata_version_safe in asset_name or masterdata_version_safe in asset_name.replace("/", "_"):
                    masterdata_found = True
                    break

    if masterdata_found:
        print(f"  Masterdata Status      : FOUND (Masterdata '{masterdata_version_safe}' exists in GitHub Releases)")
    else:
        print(f"  Masterdata Status      : NOT FOUND (Masterdata '{masterdata_version_safe}' missing from releases)")

    if catalog_release is None:
        print(f"  Analysis Result        : RELEASE_BOTH -> Catalog version '{catalog_version}' is new. Will release both assets & masterdata.")
        return "RELEASE_BOTH", catalog_version

    if not masterdata_found:
        next_inc = max(increment_nums) + 1 if increment_nums else 1
        new_tag = f"{catalog_version}-{next_inc}"
        print(f"  Analysis Result        : RELEASE_MASTERDATA_ONLY -> Catalog is up-to-date, but new masterdata detected. Will release masterdata under incremental tag '{new_tag}'.")
        return "RELEASE_MASTERDATA_ONLY", new_tag

    print("  Analysis Result        : SKIP -> Both catalog and masterdata are already up-to-date in GitHub Releases.")
    return "SKIP", catalog_version


def create_github_release(
    tag_name: str, release_title: str, release_notes: str, files_to_upload: list[Path]
) -> None:
    """Create GitHub Release using `gh` CLI."""
    cmd = [
        "gh",
        "release",
        "create",
        tag_name,
        "--title",
        release_title,
        "--notes",
        release_notes,
    ]
    for f in files_to_upload:
        cmd.append(str(f))

    print(f"\nPublishing GitHub Release '{tag_name}'...")
    print(f"Files to upload ({len(files_to_upload)}):")
    for f in files_to_upload:
        size_mb = f.stat().st_size / (1024 * 1024) if f.exists() else 0
        print(f"  - {f.name} ({size_mb:.2f} MB)")
    print(f"Executing Command: {' '.join(cmd)}")

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"gh release create failed (Exit Code {res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        sys.exit(res.returncode)
    print(f"Successfully published release '{tag_name}'!")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="dry run check only without downloading or releasing")
    args = parser.parse_args()

    print("=== Checking Masterdata & Asset Catalog Versions ===")
    masterdata_version_raw, _ = get_masterdata_version()
    # Strip URL parameters to get masterdata_version e.g. "2026-07-28/mastermemory_1785151010_1785151010.db"
    masterdata_version = masterdata_version_raw.split("?")[0]
    catalog_version = get_catalog_version()

    print(f"masterdata_version = {masterdata_version}")
    print(f"catalog_version    = {catalog_version}")

    masterdata_version_safe = masterdata_version.replace("/", "_")
    masterdata_zip_filename = f"{masterdata_version_safe}.zip"

    releases = fetch_github_releases()
    action, release_tag = analyze_release_status(
        releases, catalog_version, masterdata_version_safe
    )

    print(f"\nAction Decision: {action} (Release Tag: {release_tag})")

    if args.dry_run:
        print("Dry-run requested. Exiting without making changes.")
        return

    if action == "SKIP":
        print("No assets or masterdata are outdated. Ending Actions.")
        return

    output_dir = project_root / "output_releases"
    output_dir.mkdir(parents=True, exist_ok=True)
    files_to_upload: list[Path] = []

    if action in ("RELEASE_BOTH", "RELEASE_MASTERDATA_ONLY"):
        print("\n--- Processing Masterdata ---")
        masterdata_dir = project_root / "_data" / "masterdata"
        download_and_unpack_masterdata(masterdata_dir)

        masterdata_zips = create_zips_from_dir(masterdata_dir, output_dir, masterdata_version_safe)
        files_to_upload.extend(masterdata_zips)

    if action == "RELEASE_BOTH":
        print("\n--- Processing Asset Catalogs & Bundles ---")
        assets_dir = project_root / "_data" / "assets"
        download_catalogs(assets_dir)

        print("\nDownloading, packaging, and cleaning Asset Bundles per kind & platform...")
        for kind in KINDS:
            for platform in PLATFORMS:
                source_dir = assets_dir / kind / platform.lower()
                base_name = f"{kind}-{platform.lower()}"
                print(f"\n--- Processing {kind} ({platform}) ---")
                download_assets_for_kind_platform(kind, platform, assets_dir=assets_dir)

                if source_dir.exists():
                    zips = create_zips_from_dir(source_dir, output_dir, base_name)
                    files_to_upload.extend(zips)

                    print(f"Removing source directory to free disk space: {source_dir}")
                    shutil.rmtree(source_dir, ignore_errors=True)

    print(f"\n--- Creating GitHub Release '{release_tag}' ---")
    notes = (
        f"Automated Release:\n"
        f"- Asset Catalog Version: `{catalog_version}`\n"
        f"- Masterdata Version: `{masterdata_version}`"
    )
    create_github_release(
        tag_name=release_tag,
        release_title=release_tag,
        release_notes=notes,
        files_to_upload=files_to_upload,
    )


if __name__ == "__main__":
    main()

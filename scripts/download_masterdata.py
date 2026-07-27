"""Download the current MasterMemory master-data blob and unpack it into per-table JSON.

    python -m instance.scripts.download_masterdata
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

# Setup import path to locate project root
current_dir = Path(__file__).resolve().parent
instance_dir = current_dir.parent
project_root = instance_dir.parent
for p in [str(project_root), str(instance_dir), str(current_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from helpers.mastermemory import unpack  # noqa: E402
from helpers.msgpack import from_array  # noqa: E402
from models.master_data import TABLES  # noqa: E402
from scripts._sirius import MaintenanceError, master_data_manifest  # noqa: E402
from models import MasterDataManifest  # noqa: E402

OUT = project_root / "_data" / "masterdata"


def _download_url(manifest: MasterDataManifest) -> str:
    uri, sas = manifest.uri or "", manifest.sas_token or ""
    if uri and sas:
        sep = "&" if "?" in uri else "?"
        return f"{uri}{sep}{sas.lstrip('?')}"
    return uri


def get_masterdata_version() -> tuple[str, str]:
    """Returns (masterdata_version, raw_download_url)."""
    manifest: MasterDataManifest = master_data_manifest()
    url_raw = _download_url(manifest)
    masterdata_version = url_raw.split("?")[0]
    return masterdata_version, url_raw


def download_and_unpack_masterdata(out_dir: Path = OUT) -> str:
    manifest: MasterDataManifest = master_data_manifest()
    url_raw = _download_url(manifest)
    masterdata_version = url_raw.split("?")[0]

    url = (
        "https://assets-e.wds-stellarium.com/master-data/production/"
        + url_raw
    )
    print(
        f"master-data version {manifest.version} (publish {manifest.publish_timestamp})"
    )
    print(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "server-of-dreams"})
    db = urllib.request.urlopen(req, timeout=120).read()
    print(f"downloaded {len(db)} bytes")

    tables = unpack(db)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        model = TABLES.get(name)
        if model is None:
            continue
        keyed = [
            from_array(model.__name__, row).model_dump(mode="json", by_alias=True)
            for row in rows
        ]
        (out_dir / f"{name}.json").write_text(
            json.dumps(keyed, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(f"unpacked {len(tables)} tables -> {out_dir}")
    return masterdata_version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None, help="unpack a local blob instead")
    args = parser.parse_args()

    if args.file:
        db = Path(args.file).read_bytes()
        print(f"read {args.file} ({len(db)} bytes)")
        tables = unpack(db)
        OUT.mkdir(parents=True, exist_ok=True)
        for name, rows in tables.items():
            model = TABLES.get(name)
            if model is None:
                continue
            keyed = [
                from_array(model.__name__, row).model_dump(mode="json", by_alias=True)
                for row in rows
            ]
            (OUT / f"{name}.json").write_text(
                json.dumps(keyed, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        print(f"unpacked {len(tables)} tables -> {OUT}")
    else:
        try:
            download_and_unpack_masterdata()
        except MaintenanceError:
            print("Server is in maintenance")


if __name__ == "__main__":
    main()

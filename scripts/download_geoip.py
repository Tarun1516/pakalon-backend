#!/usr/bin/env python3
"""
download_geoip.py — Download the MaxMind GeoLite2-City database.

Usage:
    python scripts/download_geoip.py --key <MAXMIND_LICENSE_KEY> [--dest /path/to/GeoLite2-City.mmdb]

MaxMind GeoLite2 is free with registration:
    https://www.maxmind.com/en/geolite2/signup

After downloading, set the environment variable in your .env:
    GEOIP_DB_PATH=/path/to/GeoLite2-City.mmdb

If you don't want to use MaxMind, the backend automatically falls back to
ip-api.com (free, no key, 45 req/min) when GEOIP_DB_PATH is not set.
"""
from __future__ import annotations

import argparse
import gzip
import io
import os
import pathlib
import shutil
import sys
import tarfile
import urllib.request

# MaxMind GeoLite2-City download URL template
_DOWNLOAD_URL = (
    "https://download.maxmind.com/app/geoip_download"
    "?edition_id=GeoLite2-City"
    "&license_key={license_key}"
    "&suffix=tar.gz"
)

_DEFAULT_DEST = pathlib.Path.home() / ".config" / "pakalon" / "GeoLite2-City.mmdb"


def download_geoip(license_key: str, dest: pathlib.Path = _DEFAULT_DEST) -> pathlib.Path:
    """
    Download the GeoLite2-City database and extract the .mmdb file to `dest`.

    Args:
        license_key: MaxMind license key (from https://www.maxmind.com/en/accounts).
        dest:        Destination .mmdb file path.

    Returns:
        Path to the downloaded .mmdb file.
    """
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = _DOWNLOAD_URL.format(license_key=license_key)
    print(f"Downloading GeoLite2-City from MaxMind...\n  URL: {url[:80]}...")

    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read()
    except Exception as exc:
        print(f"ERROR: Download failed — {exc}", file=sys.stderr)
        print(
            "\nTroubleshooting:\n"
            "  1. Verify your license key at https://www.maxmind.com/en/accounts\n"
            "  2. Make sure you've accepted the GeoLite2 EULA in your account\n"
            "  3. Check your network/proxy settings\n"
            "\nFallback: the backend will use ip-api.com automatically if GEOIP_DB_PATH is unset.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Extract .mmdb from the .tar.gz archive
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        mmdb_member = next(
            (m for m in tf.getmembers() if m.name.endswith(".mmdb")),
            None,
        )
        if mmdb_member is None:
            print("ERROR: No .mmdb file found in archive.", file=sys.stderr)
            sys.exit(1)

        extracted = tf.extractfile(mmdb_member)
        if extracted is None:
            print("ERROR: Could not extract .mmdb file.", file=sys.stderr)
            sys.exit(1)

        dest.write_bytes(extracted.read())

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"\n✓ GeoLite2-City database saved to:\n  {dest}\n  ({size_mb:.1f} MB)")
    print(
        "\nTo activate, add this to your .env file:\n"
        f"  GEOIP_DB_PATH={dest}\n"
        "\nOr export it in your shell:\n"
        f"  export GEOIP_DB_PATH={dest}"
    )
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download the MaxMind GeoLite2-City database for IP geolocation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--key",
        required=False,
        metavar="LICENSE_KEY",
        help="MaxMind license key (also reads MAXMIND_LICENSE_KEY env var)",
    )
    parser.add_argument(
        "--dest",
        default=str(_DEFAULT_DEST),
        metavar="PATH",
        help=f"Destination path for the .mmdb file (default: {_DEFAULT_DEST})",
    )
    args = parser.parse_args()

    license_key = args.key or os.environ.get("MAXMIND_LICENSE_KEY", "")
    if not license_key:
        print(
            "ERROR: MaxMind license key required.\n"
            "  Supply via --key or MAXMIND_LICENSE_KEY env var.\n"
            "  Register free at: https://www.maxmind.com/en/geolite2/signup\n"
            "\nNo MaxMind key? No problem — the backend automatically uses\n"
            "ip-api.com (free, no key needed) when GEOIP_DB_PATH is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    download_geoip(license_key, pathlib.Path(args.dest))


if __name__ == "__main__":
    main()

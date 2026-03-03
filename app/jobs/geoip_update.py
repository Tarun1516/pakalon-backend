"""
GeoIP DB auto-update job (T-BE-21).

Downloads a fresh MaxMind GeoLite2-City database once per week, replacing the
file at `settings.geoip_db_path` in-place.  Falls back silently — if
MaxMind is unreachable or the license key is absent, the existing DB (or
ip-api.com fallback) continues to work.
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import tarfile
import urllib.request
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_MAXMIND_URL = (
    "https://download.maxmind.com/app/geoip_download"
    "?edition_id=GeoLite2-City"
    "&license_key={license_key}"
    "&suffix=tar.gz"
)


def _download_and_replace(license_key: str, dest: Path) -> None:
    """Download GeoLite2-City tar.gz and extract .mmdb to *dest*."""
    url = _MAXMIND_URL.format(license_key=license_key)
    logger.info("[GeoIP] Downloading GeoLite2-City from MaxMind…")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            raw = resp.read()
    except Exception as exc:
        logger.warning("[GeoIP] Download failed: %s — keeping existing DB", exc)
        return

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            mmdb_member = next(
                (m for m in tf.getmembers() if m.name.endswith(".mmdb")),
                None,
            )
            if mmdb_member is None:
                logger.error("[GeoIP] No .mmdb in MaxMind archive — aborting update")
                return

            extracted = tf.extractfile(mmdb_member)
            if extracted is None:
                logger.error("[GeoIP] Could not extract .mmdb member — aborting update")
                return

            data = extracted.read()

        # Write to a temp file, then atomic-rename over the destination
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".mmdb.tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        logger.info("[GeoIP] Updated %s (%d bytes)", dest, len(data))
    except Exception as exc:
        logger.exception("[GeoIP] Failed to extract/replace DB: %s", exc)


async def run_geoip_update() -> None:
    """
    APScheduler job entrypoint.

    Skips gracefully if:
    - MAXMIND_LICENSE_KEY env / settings.maxmind_license_key is absent.
    - GEOIP_DB_PATH is not configured (no local DB in use).
    """
    settings = get_settings()
    license_key = settings.maxmind_license_key or os.getenv("MAXMIND_LICENSE_KEY", "")
    if not license_key:
        logger.debug("[GeoIP] MAXMIND_LICENSE_KEY not set — skipping weekly update")
        return

    geoip_path = settings.geoip_db_path
    if not geoip_path:
        logger.debug("[GeoIP] GEOIP_DB_PATH not set — skipping weekly update")
        return

    dest = Path(geoip_path)
    _download_and_replace(license_key, dest)

"""Shared enrichment orchestrator — used by both CLI and WebUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scanner import SeriesScan
from tmdb_api import enrich_with_tmdb
from bangumi_api import enrich_with_bangumi
from anilist_api import enrich_with_anilist
from anidb_api import (
    AniDBUdpClient,
    enrich_with_anidb,
    enrich_scans_with_anidb_title_dump,
)


def confidence_band(score: float) -> str:
    if score >= 0.90:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


def enrich_all_sources(
    scan: SeriesScan,
    *,
    tmdb_api_key: str = "",
    tmdb_language: str = "ja-JP",
    anidb_cache: Path | None = None,
    anidb_client: AniDBUdpClient | None = None,
    anidb_username: str = "",
    anidb_password: str = "",
    enable_bangumi: bool = True,
    enable_anilist: bool = True,
    enable_tmdb: bool = True,
    enable_anidb_udp: bool = False,
    enable_anidb_title_dump: bool = True,
) -> None:
    """Enrich a single scan with all enabled metadata sources.

    This replaces the repeated 4-5 block pattern that existed in both
    CLI main() and WebUI scan/preview endpoints.
    """
    if enable_tmdb and tmdb_api_key:
        enrich_with_tmdb(scan, tmdb_api_key, tmdb_language)

    if enable_bangumi:
        enrich_with_bangumi(scan)

    if enable_anilist:
        enrich_with_anilist(scan)

    if enable_anidb_title_dump and anidb_cache:
        enrich_scans_with_anidb_title_dump([scan], anidb_cache)

    if enable_anidb_udp:
        if anidb_client:
            from anidb_api import enrich_with_anidb_client
            enrich_with_anidb_client(scan, anidb_client)
        elif anidb_username and anidb_password:
            enrich_with_anidb(scan, anidb_username, anidb_password)


def enrich_all_scans(
    scans: list[SeriesScan],
    *,
    tmdb_api_key: str = "",
    tmdb_language: str = "ja-JP",
    anidb_cache: Path | None = None,
    anidb_username: str = "",
    anidb_password: str = "",
    enable_bangumi: bool = True,
    enable_anilist: bool = True,
    enable_tmdb: bool = True,
    enable_anidb_udp: bool = False,
    enable_anidb_title_dump: bool = True,
) -> None:
    """Enrich multiple scans with all enabled metadata sources — batch version."""
    eligible = [s for s in scans if s.structure in {"episode_subdirs", "season_dirs"} and s.episode_count]

    if enable_tmdb and tmdb_api_key:
        for scan in eligible:
            enrich_with_tmdb(scan, tmdb_api_key, tmdb_language)
    elif enable_tmdb:
        for scan in eligible:
            scan.reason_flags.append("tmdb_api_key_missing")

    if enable_bangumi:
        from bangumi_api import enrich_scans_with_bangumi
        enrich_scans_with_bangumi(eligible)

    if enable_anilist:
        from anilist_api import enrich_scans_with_anilist
        enrich_scans_with_anilist(eligible)

    if enable_anidb_title_dump and anidb_cache:
        enrich_scans_with_anidb_title_dump(eligible, anidb_cache)

    if enable_anidb_udp and anidb_username and anidb_password:
        from anidb_api import enrich_scans_with_anidb
        enrich_scans_with_anidb(eligible, anidb_username, anidb_password)
    elif enable_anidb_udp:
        for scan in eligible:
            scan.reason_flags.append("anidb_udp_credentials_missing")

#!/usr/bin/env python3
"""CLI entry point for scanning media folders and building metadata preview manifests.

The heavy logic is split across:
  text_utils.py  — romaji, fuzzy matching, text normalization
  scanner.py    — directory scanning, structure detection, dataclasses
  scoring.py    — shared scoring weights and thresholds
  tmdb_api.py   — TMDB search, series/season details, candidate scoring
  bangumi_api.py — Bangumi search, subject details, candidate scoring
  anilist_api.py — AniList GraphQL search, candidate scoring
  anidb_api.py  — AniDB UDP client, title dump matching
  enrich.py     — enrich_all_sources() shared orchestrator
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Re-export for backward compatibility (app_webui.py, tmdb_rename.py)
from scanner import (
    MEDIA_EXTS,
    EPISODE_DIR_RE,
    SEASON_DIR_RE,
    Candidate,
    EpisodeItem,
    SeriesScan,
    guess_structure,
    scan_series,
    title_variants,
)
from text_utils import (
    to_romaji,
    fuzzy_match_score,
    levenshtein_ratio,
    normalize_for_match,
    clean_text,
    extract_episode_token,
    _has_kana,
    JUNK_PATTERNS,
)
from scoring import (
    WEIGHT_TITLE, WEIGHT_EPISODE_COUNT, WEIGHT_CONTINUITY,
    BONUS_STRUCTURE, PENALTY_OVA, SCORE_THRESHOLD,
)
from tmdb_api import (
    search_tmdb,
    tmdb_season_details,
    tmdb_series_details,
    score_candidate,
    enrich_with_tmdb,
)
from bangumi_api import (
    enrich_scans_with_bangumi,
    enrich_with_bangumi,
    score_bangumi_candidate,
    bangumi_subject_details,
    bangumi_episodes_to_map,
)
from anilist_api import (
    enrich_scans_with_anilist,
    enrich_with_anilist,
    score_anilist_candidate,
)
from anidb_api import (
    ANIDB_TITLES_URL,
    XML_NS,
    AniDBUdpClient,
    anidb_escape,
    enrich_with_anidb,
    enrich_scans_with_anidb,
    ensure_anidb_title_dump,
    enrich_scans_with_anidb_title_dump,
    read_anidb_auth_file,
)
from enrich import enrich_all_sources, enrich_all_scans, confidence_band


# ── CLI-only utilities ──────────────────────────────────────────────

JUNK_DIR_NAMES = {"__pycache__", "anidb-title-cache", ".codex", ".omx", ".tmdb-apply-test", ".tmdb-cli-test"}


def render_csv(scans: list[SeriesScan], path: Path, extra_items: list[dict[str, Any]] | None = None) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "path", "structure", "title_hint", "episode_count", "local_confidence",
            "top_candidate_name", "top_candidate_score", "top_candidate_band", "reason_flags",
        ])
        for scan in scans:
            top = scan.candidates[0] if scan.candidates else None
            writer.writerow([
                scan.path, scan.structure, scan.title_hint, scan.episode_count,
                scan.confidence,
                top.name if top else "", top.score if top else "",
                confidence_band(top.score) if top else "",
                ";".join(scan.reason_flags),
            ])
        for item in (extra_items or []):
            top = item.get("top_candidate") or {}
            fp = item.get("fingerprint") or {}
            writer.writerow([
                item.get("path", ""), item.get("structure", ""), item.get("title_hint", ""),
                fp.get("episode_count", item.get("episode_count", "")),
                item.get("confidence", ""),
                top.get("name", ""), top.get("score", ""),
                item.get("top_candidate_band", ""),
                ";".join(item.get("reason_flags", [])),
            ])


def manifest_item(scan: SeriesScan) -> dict[str, Any]:
    data = asdict(scan)
    top = scan.candidates[0] if scan.candidates else None
    data["top_candidate_band"] = confidence_band(top.score) if top else ""
    data["recommended_action"] = (
        "review"
        if (scan.reason_flags or not top or confidence_band(top.score) != "high")
        else "eligible_for_batch_approval"
    )
    return data


def series_paths(root: Path, explicit_paths: list[str]) -> list[Path]:
    if explicit_paths:
        return [Path(p) for p in explicit_paths]
    return sorted([p for p in root.iterdir() if p.is_dir() and p.name not in JUNK_DIR_NAMES])


def load_previous_manifest(json_path: Path) -> dict[str, dict[str, Any]]:
    if not json_path.exists():
        return {}
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        result: dict[str, dict[str, Any]] = {}
        for item in data.get("items", []):
            path_str = item.get("path", "")
            result[path_str] = item
            folder_name = Path(path_str).name
            if folder_name and folder_name not in result:
                result[f"__name__{folder_name}"] = item
        return result
    except Exception:
        return {}


def quick_fingerprint(path: Path) -> dict[str, Any]:
    try:
        entries = list(path.iterdir())
    except OSError:
        return {}
    entry_count = len(entries)
    structure = guess_structure(path)
    child_dirs = [p for p in entries if p.is_dir()]
    child_media = [p for p in entries if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    episode_count = 0
    if structure == "episode_subdirs":
        episode_count = sum(1 for d in child_dirs if EPISODE_DIR_RE.match(d.name))
    elif structure == "season_dirs":
        for sd in child_dirs:
            if SEASON_DIR_RE.match(sd.name):
                try:
                    episode_count += len([f for f in sd.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXTS])
                except OSError:
                    pass
    elif structure == "flat":
        episode_count = len([f for f in child_media if f.suffix.lower() not in {".srt", ".ass", ".ssa", ".sub"}])
    try:
        newest_mtime = max(p.stat().st_mtime for p in entries)
    except OSError:
        newest_mtime = 0.0
    # hash of all file/subdir names to detect renames
    all_names = sorted(p.name for p in entries)
    name_hash = hash(tuple(all_names))
    return {
        "structure": structure,
        "entry_count": entry_count,
        "episode_count": episode_count,
        "newest_mtime": newest_mtime,
        "name_hash": name_hash,
    }


def fingerprint_changed(prev_item: dict[str, Any], current_fp: dict[str, Any]) -> bool:
    prev_fp = prev_item.get("fingerprint", {})
    if not prev_fp:
        return True
    for key in ("structure", "entry_count", "episode_count"):
        if prev_fp.get(key) != current_fp.get(key):
            return True
    prev_eps = prev_fp.get("episode_numbers", [])
    cur_eps = current_fp.get("episode_numbers", [])
    if prev_eps != cur_eps:
        return True
    # detect file renames (same count but different names)
    if prev_fp.get("name_hash") != current_fp.get("name_hash"):
        return True
    return False


# ── CLI main ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Scan media folders and build a metadata preview manifest.")
    parser.add_argument("--root", default=".", help="Library root directory")
    parser.add_argument("--path", action="append", default=[], help="Specific series path(s) to scan")
    parser.add_argument("--tmdb-api-key", default=os.environ.get("TMDB_API_KEY", ""), help="TMDB v4 bearer token; defaults to TMDB_API_KEY")
    parser.add_argument("--source", choices=["tmdb", "anidb", "both"], default="tmdb", help="Metadata source to use for matching")
    parser.add_argument("--anidb-username", default=os.environ.get("ANIDB_USERNAME", ""), help="AniDB username; defaults to ANIDB_USERNAME")
    parser.add_argument("--anidb-password", default=os.environ.get("ANIDB_PASSWORD", ""), help="AniDB password; defaults to ANIDB_PASSWORD")
    parser.add_argument("--anidb-auth-file", default="anidb-auth.txt", help="Local AniDB credential file if username/password are omitted")
    parser.add_argument("--anidb-client", default="myjbrename", help="AniDB registered UDP API client name")
    parser.add_argument("--anidb-client-version", type=int, default=1, help="AniDB registered UDP API client version")
    parser.add_argument("--anidb-min-interval", type=float, default=4.0, help="Minimum seconds between AniDB UDP requests")
    parser.add_argument("--anidb-timeout", type=float, default=30.0, help="AniDB UDP socket timeout in seconds")
    parser.add_argument("--anidb-retries", type=int, default=1, help="AniDB UDP retry count per command")
    parser.add_argument("--anidb-title-cache", default="anidb-title-cache/anime-titles.xml.gz", help="Local cache path for AniDB anime title dump")
    parser.add_argument("--anidb-title-cache-max-age-hours", type=float, default=24.0)
    parser.add_argument("--no-anidb-title-dump", action="store_true", help="Disable AniDB title dump matching")
    parser.add_argument("--language", default="ja-JP", help="TMDB language for search/season lookups")
    parser.add_argument("--output-json", default="tmdb_preview_manifest.json", help="Where to write JSON manifest")
    parser.add_argument("--output-csv", default="tmdb_preview_manifest.csv", help="Where to write CSV summary")
    parser.add_argument("--load-manifest", default="", help="Load previous results from this JSON (default: same as --output-json)")
    parser.add_argument("--incremental", action="store_true", help="Skip unchanged high-confidence matches from previous manifest")
    parser.add_argument("--skip-matched", action="store_true", help="Skip all previously matched items (any confidence)")
    parser.add_argument("--skip-status", action="append", default=[], help="Skip items with these bands from previous manifest")
    parser.add_argument("--force-rescan", action="store_true", help="Force re-scan even for unchanged items")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    json_path = Path(args.output_json).resolve()
    load_path = Path(args.load_manifest).resolve() if args.load_manifest else json_path

    prev_manifest = load_previous_manifest(load_path)
    reused_items: list[dict[str, Any]] = []

    all_paths = series_paths(root, args.path)
    scan_paths: list[Path] = []
    for p in all_paths:
        p_str = str(p.resolve())
        name_key = f"__name__{p.name}"
        prev_item = prev_manifest.get(p_str) or prev_manifest.get(name_key)

        if not args.force_rescan:
            if args.skip_matched and (p / "tvshow.nfo").exists():
                reused_items.append({
                    "path": p_str,
                    "structure": guess_structure(p),
                    "title_hint": p.name,
                    "episode_count": 0,
                    "confidence": "high",
                    "reason_flags": ["reused_tvshow_nfo"],
                    "candidates": [],
                    "top_candidate_band": "high",
                    "recommended_action": "skip",
                })
                continue

            if prev_item:
                prev_band = prev_item.get("top_candidate_band", "")
                prev_candidates = prev_item.get("candidates") or []
                if args.skip_matched and prev_candidates:
                    reused_items.append(prev_item)
                    continue
                if args.skip_status and prev_band in args.skip_status:
                    reused_items.append(prev_item)
                    continue
                if args.incremental and prev_band == "high":
                    fp = quick_fingerprint(p)
                    if not fingerprint_changed(prev_item, fp):
                        reused_items.append(prev_item)
                        continue
        scan_paths.append(p)

    if reused_items:
        print(f"Skipping {len(reused_items)} unchanged/matched items, scanning {len(scan_paths)}")
    scans = [scan_series(path.resolve()) for path in scan_paths]

    anidb_username = args.anidb_username.strip()
    anidb_password = args.anidb_password.strip()
    if args.source in {"anidb", "both"} and (not anidb_username or not anidb_password):
        auth_file = Path(args.anidb_auth_file)
        if not auth_file.is_absolute():
            auth_file = root / auth_file
        if auth_file.exists():
            file_username, file_password = read_anidb_auth_file(auth_file)
            anidb_username = anidb_username or file_username
            anidb_password = anidb_password or file_password

    title_cache = Path(args.anidb_title_cache)
    if not title_cache.is_absolute():
        title_cache = root / title_cache

    # Use the shared enrichment layer
    enrich_all_scans(
        scans,
        tmdb_api_key=args.tmdb_api_key,
        tmdb_language=args.language,
        anidb_cache=title_cache if not args.no_anidb_title_dump else None,
        anidb_username=anidb_username,
        anidb_password=anidb_password,
        enable_tmdb=args.source in {"tmdb", "both"},
        enable_anidb_udp=args.source in {"anidb", "both"},
        enable_anidb_title_dump=args.source in {"anidb", "both"} and not args.no_anidb_title_dump,
    )

    all_items = [manifest_item(scan) for scan in scans] + reused_items
    all_items.sort(key=lambda item: item["path"])

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root),
        "source": args.source,
        "tmdb_enabled": args.source in {"tmdb", "both"} and bool(args.tmdb_api_key),
        "anidb_enabled": args.source in {"anidb", "both"} and (not args.no_anidb_title_dump or bool(anidb_username and anidb_password)),
        "items": all_items,
    }

    csv_path = Path(args.output_csv).resolve()
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    render_csv(scans, csv_path, extra_items=reused_items)

    print(f"Wrote JSON manifest: {json_path}")
    print(f"Wrote CSV summary:   {csv_path}")
    print(f"Scanned items:       {len(scans)}")
    print(f"Reused (skipped):    {len(reused_items)}")
    print(f"Total items:         {len(all_items)}")
    matched = sum(1 for scan in scans if scan.candidates)
    print(f"Items with metadata candidates: {matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

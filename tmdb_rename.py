#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tmdb_scan_preview import (
    MEDIA_EXTS,
    bangumi_episodes_to_map,
    bangumi_subject_details,
    enrich_scans_with_anidb,
    enrich_scans_with_anidb_title_dump,
    enrich_scans_with_bangumi,
    enrich_scans_with_anilist,
    enrich_with_tmdb,
    read_anidb_auth_file,
    scan_series,
    series_paths,
    tmdb_season_details,
    tmdb_series_details,
)


@dataclass
class PersonInfo:
    """Actor / crew member from a metadata source."""
    name: str
    role: str = ""       # "Voice Actor", "Director", "Writer"
    character: str = ""  # character name (for voice actors)
    thumb: str = ""      # photo URL
    order: int = 0


@dataclass
class EpisodeMetadata:
    """Single episode metadata for NFO generation."""
    season: int
    episode: int
    title: str = ""
    title_cn: str = ""   # Chinese episode title (Bangumi / AniDB)
    overview: str = ""
    air_date: str = ""
    rating: str = ""
    still_url: str = ""


@dataclass
class SeriesMetadata:
    """Unified series metadata — all sources normalize into this."""
    title: str
    original_title: str = ""
    sort_title: str = ""
    overview: str = ""
    first_air_date: str = ""
    year: str = ""
    status: str = ""
    rating: str = ""
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    actors: list[PersonInfo] = field(default_factory=list)
    directors: list[PersonInfo] = field(default_factory=list)
    poster_url: str = ""
    backdrop_urls: list[str] = field(default_factory=list)
    episodes: list[EpisodeMetadata] = field(default_factory=list)
    source: str = ""       # "tmdb", "bangumi", "anilist"
    source_id: str = ""


INVALID_FILENAME_CHARS = str.maketrans(
    {
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
)


def safe_name(value: str) -> str:
    value = value.translate(INVALID_FILENAME_CHARS)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.rstrip(" .")
    return value


def choose_series_title(scan, source: str) -> str:
    top = scan.candidates[0] if scan.candidates else None
    if source in {"metadata", "tmdb"} and top and top.name.strip():
        return safe_name(top.name.strip())
    # fallback：使用目录名作为系列名（无论是否有候选者都可用）
    return safe_name(scan.title_hint.strip())


def canonical_file_name(series_title: str, episode_index: int, title_hint: str, suffix: str, season_number: int = 1) -> str:
    name = f"{series_title} - S{season_number:02d}E{episode_index:02d}"
    title_hint = safe_name(title_hint)
    if title_hint:
        name += f" - {title_hint}"
    return f"{name}{suffix.lower()}"


def looks_canonical(file_name: str, series_title: str, episode_index: int, season_number: int = 1) -> bool:
    prefix = re.escape(series_title)
    return bool(re.match(rf"^{prefix} - S{season_number:02d}E{episode_index:02d}(?: - .+)?\.[^.]+$", file_name))


def plan_episode(scan, episode, series_title: str) -> list[dict[str, Any]]:
    if episode.episode_dir == ".":
        # flat 结构：使用 episode 预收集的媒体文件
        episode_dir = Path(scan.path)
        media_files = [episode_dir / fname for fname in episode.media_files]
        media_files = sorted([p for p in media_files if p.is_file() and p.suffix.lower() in MEDIA_EXTS])
    else:
        episode_dir = Path(scan.path) / episode.episode_dir
        if not episode_dir.exists():
            return [{"status": "error", "path": str(episode_dir), "reason": "episode_dir_missing"}]
        media_files = sorted([p for p in episode_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS])
    ext_counts: dict[str, int] = {}
    for file in media_files:
        ext_counts[file.suffix.lower()] = ext_counts.get(file.suffix.lower(), 0) + 1

    title_hint = episode.local_title_hint or ""

    season_number = getattr(episode, "season_number", 1)

    results: list[dict[str, Any]] = []
    for file in media_files:
        suffix = file.suffix.lower()
        if ext_counts[suffix] > 1:
            results.append({
                "status": "skip",
                "path": str(file),
                "reason": f"multiple_files_same_extension:{suffix}",
            })
            continue
        target_name = canonical_file_name(series_title, episode.index, title_hint, suffix, season_number)
        target_path = file.with_name(target_name)
        if file.name == target_name or looks_canonical(file.name, series_title, episode.index, season_number):
            results.append({
                "status": "noop",
                "path": str(file),
                "target": str(target_path),
                "reason": "already_canonical",
            })
            continue
        if target_path.exists() and target_path != file:
            results.append({
                "status": "error",
                "path": str(file),
                "target": str(target_path),
                "reason": "target_exists",
            })
            continue
        results.append({
            "status": "rename",
            "path": str(file),
            "target": str(target_path),
            "reason": "matched_candidate",
        })
    return results


def evaluate_scan(scan, min_score: float, series_title_source: str, allow_fallback: bool = False) -> dict[str, Any]:
    top = scan.candidates[0] if scan.candidates else None
    if not top:
        if allow_fallback:
            # 无数据时用目录名作为系列名重命名
            series_title = choose_series_title(scan, "local")
            operations: list[dict[str, Any]] = []
            for episode in scan.episodes:
                operations.extend(plan_episode(scan, episode, series_title))
            rename_count = sum(1 for op in operations if op["status"] == "rename")
            error_count = sum(1 for op in operations if op["status"] == "error")
            return {
                "path": scan.path,
                "status": "rename" if rename_count else ("error" if error_count else "noop"),
                "reason": "fallback_no_metadata",
                "reason_flags": scan.reason_flags,
                "top_candidate": None,
                "series_title": series_title,
                "operations": operations,
            }
        return {
            "path": scan.path,
            "status": "unmatched",
            "reason": "no_metadata_candidate",
            "reason_flags": scan.reason_flags,
            "top_candidate": None,
            "operations": [],
        }
    if top.score < min_score:
        return {
            "path": scan.path,
            "status": "skip",
            "reason": f"score_below_threshold:{top.score}",
            "reason_flags": scan.reason_flags,
            "top_candidate": asdict(top),
            "operations": [],
        }
    if scan.structure not in {"episode_subdirs", "season_dirs", "flat"}:
        series_title = choose_series_title(scan, series_title_source)
        return {
            "path": scan.path,
            "status": "skip",
            "reason": f"unsupported_structure:{scan.structure}",
            "reason_flags": scan.reason_flags,
            "top_candidate": asdict(top),
            "series_title": series_title,
            "operations": [],
        }

    series_title = choose_series_title(scan, series_title_source)
    operations: list[dict[str, Any]] = []
    for episode in scan.episodes:
        operations.extend(plan_episode(scan, episode, series_title))

    # Folder rename: prefer Chinese title if available, else use series_title
    folder_title = safe_name(top.zh_title) if (hasattr(top, 'zh_title') and top.zh_title) else series_title
    current_folder = Path(scan.path).name
    if folder_title and folder_title != current_folder:
        operations.append({
            "status": "rename_folder",
            "path": str(scan.path),
            "target": str(Path(scan.path).parent / folder_title),
            "reason": "matched_candidate",
        })

    rename_count = sum(1 for op in operations if op["status"] == "rename")
    error_count = sum(1 for op in operations if op["status"] == "error")
    if error_count:
        status = "error"
    elif rename_count:
        status = "rename"
    else:
        status = "noop"
    return {
        "path": scan.path,
        "status": status,
        "reason": f"top_candidate:{top.name}",
        "reason_flags": scan.reason_flags,
        "top_candidate": asdict(top),
        "series_title": series_title,
        "operations": operations,
    }


def apply_operations(items: list[dict[str, Any]], apply: bool) -> tuple[int, int]:
    rename_total = 0
    error_total = 0
    # Collect folder rename ops — must run AFTER file renames
    # Use mutable references so errors propagate back to the report
    folder_ops: list[dict[str, Any]] = []
    for item in items:
        for op in item["operations"]:
            if op["status"] == "rename":
                print(f"PLAN: {op['path']} -> {op['target']}", flush=True)
                if apply:
                    try:
                        Path(op["path"]).rename(Path(op["target"]))
                        print(f"RENAMED: {op['path']} -> {op['target']}", flush=True)
                        rename_total += 1
                    except OSError as exc:
                        op["status"] = "error"
                        op["reason"] = f"rename_failed:{type(exc).__name__}:{exc}"
                        error_total += 1
            elif op["status"] == "rename_folder":
                folder_ops.append(op)
            elif op["status"] == "error":
                print(f"ERROR: {op['path']} :: {op['reason']}", flush=True)
                error_total += 1
    # Process folder renames after all file renames
    for op in folder_ops:
        src, dst, reason = op["path"], op["target"], op.get("reason", "")
        print(f"PLAN FOLDER: {src} -> {dst}", flush=True)
        if apply:
            try:
                Path(src).rename(Path(dst))
                print(f"RENAMED FOLDER: {src} -> {dst}", flush=True)
                rename_total += 1
            except OSError as exc:
                op["status"] = "error"
                op["reason"] = f"folder_rename_failed:{type(exc).__name__}:{exc}"
                print(f"ERROR FOLDER: {src} -> {dst} :: {exc}", flush=True)
                error_total += 1
    return rename_total, error_total


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"rename": 0, "noop": 0, "skip": 0, "unmatched": 0, "error": 0}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def generate_tvshow_nfo(meta: SeriesMetadata) -> str:
    """Generate tvshow.nfo XML from unified SeriesMetadata."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<tvshow>"]
    lines.append(f"  <title>{_xml_escape(meta.title)}</title>")
    if meta.original_title and meta.original_title != meta.title:
        lines.append(f"  <originaltitle>{_xml_escape(meta.original_title)}</originaltitle>")
    lines.append(f"  <sorttitle>{_xml_escape(meta.sort_title or meta.title)}</sorttitle>")
    if meta.rating:
        lines.append(f"  <rating>{_xml_escape(meta.rating)}</rating>")
    if meta.year:
        lines.append(f"  <year>{_xml_escape(meta.year)}</year>")
    if meta.overview:
        lines.append(f"  <plot>{_xml_escape(meta.overview)}</plot>")
    if meta.first_air_date:
        lines.append(f"  <premiered>{_xml_escape(meta.first_air_date)}</premiered>")
    if meta.status:
        lines.append(f"  <status>{_xml_escape(meta.status)}</status>")
    for g in meta.genres:
        lines.append(f"  <genre>{_xml_escape(g)}</genre>")
    if meta.studios:
        lines.append(f"  <studio>{_xml_escape(meta.studios[0])}</studio>")
    if meta.poster_url:
        lines.append(f'  <thumb aspect="poster">poster.jpg</thumb>')
    for i, url in enumerate(meta.backdrop_urls):
        fname = "fanart.jpg" if i == 0 else f"fanart{i+1}.jpg"
        if i == 0:
            lines.append(f"  <fanart>")
        lines.append(f'    <thumb>{_xml_escape(fname)}</thumb>')
    if meta.backdrop_urls:
        lines.append(f"  </fanart>")
    # Actors
    for actor in sorted(meta.actors, key=lambda a: a.order):
        lines.append(f"  <actor>")
        lines.append(f"    <name>{_xml_escape(actor.name)}</name>")
        if actor.role:
            lines.append(f"    <role>{_xml_escape(actor.role)}</role>")
        if actor.character:
            lines.append(f"    <character>{_xml_escape(actor.character)}</character>")
        lines.append(f"    <order>{actor.order}</order>")
        if actor.thumb:
            lines.append(f"    <thumb>{_xml_escape(actor.thumb)}</thumb>")
        lines.append(f"  </actor>")
    # Directors
    for d in meta.directors:
        lines.append(f"  <director>{_xml_escape(d.name)}</director>")
    # Tags (Bangumi)
    for t in meta.tags:
        lines.append(f"  <tag>{_xml_escape(t)}</tag>")
    lines.append("</tvshow>")
    return "\n".join(lines) + "\n"



def generate_episode_nfo(ep: EpisodeMetadata) -> str:
    """Generate episode NFO XML from EpisodeMetadata."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<episodedetails>"]
    title = ep.title_cn or ep.title or f"Episode {ep.episode}"
    lines.append(f"  <title>{_xml_escape(title)}</title>")
    lines.append(f"  <season>{ep.season}</season>")
    lines.append(f"  <episode>{ep.episode}</episode>")
    if ep.overview:
        lines.append(f"  <plot>{_xml_escape(ep.overview)}</plot>")
    if ep.air_date:
        lines.append(f"  <aired>{_xml_escape(ep.air_date)}</aired>")
    if ep.rating:
        lines.append(f"  <rating>{_xml_escape(ep.rating)}</rating>")
    if ep.still_url:
        fname = f"S{ep.season:02d}E{ep.episode:02d}-thumb.jpg"
        lines.append(f'  <thumb>{_xml_escape(fname)}</thumb>')
    lines.append("</episodedetails>")
    return "\n".join(lines) + "\n"


def tmdb_adapter(tmdb_id: int, season_number: int, api_key: str, language: str) -> SeriesMetadata | None:
    """Fetch TMDB series details, credits, and season episodes → SeriesMetadata."""
    details = tmdb_series_details(tmdb_id, api_key, language)
    if not details:
        return None

    # Credits (from append_to_response=credits)
    credits_data = details.get("credits") or {}

    title = details.get("name") or ""
    original_title = details.get("original_name") or ""
    overview = details.get("overview") or ""
    first_air_date = details.get("first_air_date") or ""
    year = first_air_date[:4] if first_air_date else ""
    status = details.get("status") or ""
    rating = str(details.get("vote_average") or "")

    genres = [g.get("name", "") for g in details.get("genres") or [] if g.get("name")]
    studios = [n.get("name", "") for n in details.get("networks") or [] if n.get("name")]
    poster_path = details.get("poster_path") or ""
    poster_url = f"https://image.tmdb.org/t/p/original{poster_path}" if poster_path else ""
    backdrops = details.get("images", {}).get("backdrops") or []
    backdrop_urls: list[str] = []
    for b in backdrops[:5]:
        bp = b.get("file_path") or ""
        if bp:
            backdrop_urls.append(f"https://image.tmdb.org/t/p/original{bp}")

    # Actors & directors from credits
    actors: list[PersonInfo] = []
    for i, c in enumerate(credits_data.get("cast", [])[:20]):
        name = c.get("name") or ""
        character = c.get("character") or ""
        profile = c.get("profile_path") or ""
        thumb = f"https://image.tmdb.org/t/p/original{profile}" if profile else ""
        if name:
            actors.append(PersonInfo(name=name, role="Actor", character=character, thumb=thumb, order=i))
    directors: list[PersonInfo] = []
    for i, c in enumerate(credits_data.get("crew", [])[:10]):
        job = c.get("job") or ""
        if job.lower() in ("director", "series director"):
            name = c.get("name") or ""
            profile = c.get("profile_path") or ""
            thumb = f"https://image.tmdb.org/t/p/original{profile}" if profile else ""
            if name:
                directors.append(PersonInfo(name=name, role=job, thumb=thumb, order=i))

    # Episodes from season
    season_data = tmdb_season_details(tmdb_id, season_number, api_key, language)
    episodes: list[EpisodeMetadata] = []
    if season_data:
        for ep in season_data.get("episodes") or []:
            ep_num = ep.get("episode_number")
            if not ep_num:
                continue
            still = ep.get("still_path") or ""
            still_url = f"https://image.tmdb.org/t/p/original{still}" if still else ""
            episodes.append(EpisodeMetadata(
                season=season_number,
                episode=int(ep_num),
                title=ep.get("name") or "",
                overview=ep.get("overview") or "",
                air_date=ep.get("air_date") or "",
                rating=str(ep.get("vote_average") or ""),
                still_url=still_url,
            ))

    return SeriesMetadata(
        title=title,
        original_title=original_title,
        sort_title=title,
        overview=overview,
        first_air_date=first_air_date,
        year=year,
        status=status,
        rating=rating,
        genres=genres,
        studios=studios,
        actors=actors,
        directors=directors,
        poster_url=poster_url,
        backdrop_urls=backdrop_urls,
        episodes=episodes,
        source="tmdb",
        source_id=str(tmdb_id),
    )


def bangumi_adapter(subject: dict[str, Any] | None, top_candidate: dict[str, Any] | None = None) -> SeriesMetadata | None:
    """Map Bangumi subject data to SeriesMetadata."""
    if not subject:
        return None
    top = top_candidate or {}

    name_cn = subject.get("name_cn") or ""
    name = subject.get("name") or ""
    title = name_cn or name or top.get("name", "")
    if not title:
        return None

    original_title = subject.get("name") or ""
    overview = (subject.get("summary") or "").replace("\r", "")
    air_date = subject.get("air_date") or ""
    year = air_date[:4] if air_date else ""

    rating_score = ""
    rating_obj = subject.get("rating") or {}
    if rating_obj:
        rating_score = str(rating_obj.get("score", ""))

    # Tags → genres + tags
    tag_list = subject.get("tags") or []
    genres = [t.get("name", "") for t in tag_list[:5] if t.get("name")]
    tags = [t.get("name", "") for t in tag_list if t.get("name")]

    studio = subject.get("platform", "") or ""
    studios = [studio] if studio else []

    # Images
    images = subject.get("images") or {}
    poster_url = images.get("large") or images.get("common") or ""
    backdrop_urls: list[str] = []
    if images.get("common"):
        backdrop_urls.append(images["common"])

    # Character/Voice actors (crt from subject details)
    actors: list[PersonInfo] = []
    crt_list = subject.get("crt") or []
    for i, crt in enumerate(crt_list[:20]):
        cv_name = crt.get("name_cn") or crt.get("name") or ""
        if not cv_name:
            continue
        actors_list = crt.get("actors") or []
        for j, act in enumerate(actors_list):
            actor_name = act.get("name_cn") or act.get("name") or ""
            if actor_name:
                actors.append(PersonInfo(
                    name=actor_name,
                    role="Voice Actor",
                    character=cv_name,
                    order=i * 10 + j,
                ))

    # Episodes from subject — "eps" can be int (count) or list (details)
    eps_raw = subject.get("eps")
    if isinstance(eps_raw, list):
        eps_list = eps_raw
    else:
        eps_list = []
    episode_count = subject.get("eps_count") or (eps_raw if isinstance(eps_raw, int) else len(eps_list))
    episodes: list[EpisodeMetadata] = []
    if eps_list:
        for ep_data in eps_list:
            ep_num = ep_data.get("sort") or ep_data.get("ep") or ep_data.get("type", 0)
            try:
                ep_num = int(ep_num)
            except (ValueError, TypeError):
                continue
            episodes.append(EpisodeMetadata(
                season=1,
                episode=ep_num,
                title=ep_data.get("name") or "",
                title_cn=ep_data.get("name_cn") or "",
                air_date=ep_data.get("airdate") or "",
                overview=ep_data.get("desc") or "",
            ))
    elif episode_count:
        # If no episode list but count is known, use bangumi_episodes_to_map
        from tmdb_scan_preview import bangumi_episodes_to_map
        try:
            ep_map = bangumi_episodes_to_map(subject)
            for ep_num in sorted(ep_map):
                ep = ep_map[ep_num]
                episodes.append(EpisodeMetadata(
                    season=1,
                    episode=ep_num,
                    title=ep.get("name") or "",
                    title_cn=ep.get("name_cn") or "",
                    overview=ep.get("desc") or "",
                    air_date=ep.get("airdate") or "",
                ))
        except Exception:
            pass

    return SeriesMetadata(
        title=title,
        original_title=original_title,
        sort_title=title,
        overview=overview,
        first_air_date=air_date,
        year=year,
        rating=rating_score,
        genres=genres,
        studios=studios,
        tags=tags,
        actors=actors,
        poster_url=poster_url,
        backdrop_urls=backdrop_urls,
        episodes=episodes,
        source="bangumi",
        source_id=str(subject.get("id", "")),
    )


def anilist_adapter(raw_data: dict[str, Any] | None) -> SeriesMetadata | None:
    """Map AniList raw_data (from scan candidate) → SeriesMetadata."""
    if not raw_data or not isinstance(raw_data, dict):
        return None

    title_data = raw_data.get("title") or {}
    title = title_data.get("romaji") or title_data.get("english") or ""
    if not title:
        return None
    original_title = title_data.get("native") or ""

    overview = (raw_data.get("description") or "").replace("<br>", "\n").replace("<br/>", "\n")
    # Strip HTML tags
    overview = __import__("re").sub(r"<[^>]+>", "", overview)

    year_str = raw_data.get("seasonYear") or raw_data.get("startDate", {}).get("year") or ""
    year = str(year_str) if year_str else ""
    status = raw_data.get("status") or ""

    rating = ""
    avg_score = raw_data.get("averageScore")
    if avg_score:
        rating = str(avg_score)

    genres = raw_data.get("genres") or []
    studios_data = raw_data.get("studios", {}).get("nodes") or []
    studios = [s.get("name", "") for s in studios_data if s.get("name")]

    poster_url = raw_data.get("coverImage", {}).get("large") or raw_data.get("coverImage", {}).get("extraLarge") or ""
    banner = raw_data.get("bannerImage") or ""
    backdrop_urls = [banner] if banner else []

    # AniList doesn't provide per-episode data in search results — empty list
    episodes: list[EpisodeMetadata] = []

    return SeriesMetadata(
        title=title,
        original_title=original_title,
        sort_title=title,
        overview=overview,
        year=year,
        status=status,
        rating=rating,
        genres=genres,
        studios=studios,
        poster_url=poster_url,
        backdrop_urls=backdrop_urls,
        episodes=episodes,
        source="anilist",
        source_id=str(raw_data.get("id", "")),
    )


def anidb_enrich_episodes(
    aid: int,
    meta: SeriesMetadata,
    client: Any,  # AniDBUdpClient to avoid circular import
) -> SeriesMetadata:
    """Fetch AniDB episode titles and fill meta.episodes[*].title."""
    try:
        anidb_eps = client.episodes_by_aid(aid)
    except Exception:
        return meta  # Non-critical — return as-is on failure

    if not anidb_eps:
        return meta

    # Build lookup: episode_number → AniDB title
    anidb_by_num: dict[int, str] = {}
    for ep in anidb_eps:
        ep_num = ep.get("episode_number", 0)
        if ep_num <= 0:
            continue
        title = ep.get("title_english") or ep.get("title_romaji") or ""
        if title:
            anidb_by_num[ep_num] = title

    if not anidb_by_num:
        return meta

    # Fill episode titles where we have no title or where AniDB has better data
    for ep in meta.episodes:
        anidb_title = anidb_by_num.get(ep.episode, "")
        if anidb_title and not ep.title:
            ep.title = anidb_title
        if anidb_title:
            ep.title_cn = ep.title_cn or anidb_title

    return meta


def download_images(meta: SeriesMetadata, target_dir: Path) -> list[Path]:
    """Download poster, backdrops, and episode stills to target_dir.
    Skips files that already exist. Returns list of saved paths."""
    saved: list[Path] = []

    def _download(url: str, dest: Path) -> bool:
        if not url or dest.exists():
            return False
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/*",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.read())
            return True
        except Exception as e:
            print(f"  IMG download failed: {url[:60]} → {e}", flush=True)
            return False

    target_dir.mkdir(parents=True, exist_ok=True)

    # Poster
    poster_path = target_dir / "poster.jpg"
    if _download(meta.poster_url, poster_path):
        saved.append(poster_path)
        # Emby uses folder.jpg as top-priority TV-series poster
        folder_path = target_dir / "folder.jpg"
        if not folder_path.exists():
            folder_path.write_bytes(poster_path.read_bytes())
            saved.append(folder_path)

    # Backdrops
    for i, url in enumerate(meta.backdrop_urls):
        fname = "fanart.jpg" if i == 0 else f"fanart{i+1}.jpg"
        bp = target_dir / fname
        if _download(url, bp):
            saved.append(bp)

    # Episode stills
    for ep in meta.episodes:
        if ep.still_url:
            fname = f"S{ep.season:02d}E{ep.episode:02d}-thumb.jpg"
            sp = target_dir / fname
            if _download(ep.still_url, sp):
                saved.append(sp)

    return saved


def _xml_escape(text: str) -> str:
    """Escape text for safe inclusion in XML content."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def download_and_write_nfo(
    evaluated: list[dict[str, Any]],
    scans: list[Any],
    api_key: str,
    language: str,
    apply: bool,
) -> int:
    """Download metadata and write NFO + images for matched series.
    Uses the unified metadata model: adapter → SeriesMetadata → NFO + images."""
    scan_by_path: dict[str, Any] = {}
    for scan in scans:
        scan_by_path[scan.path] = scan

    written_count = 0
    for item in evaluated:
        top = item.get("top_candidate")
        if not top:
            continue

        source = top.get("source", "")
        source_id = top.get("source_id", "")
        series_path = Path(item["path"])
        series_title = item.get("series_title", "") or top.get("name", "")
        if not series_title:
            continue

        season_fit = top.get("season_fit") or {}
        season_number = int(season_fit.get("season_number", 1))

        print(f"  NFO: [{source}] {series_title[:40]}", flush=True)

        # ── Step 1: Adapter → SeriesMetadata ─────────────────────────
        meta: SeriesMetadata | None = None

        if source == "tmdb" and api_key and top.get("tmdb_id"):
            try:
                tmdb_id = int(top["tmdb_id"])
                meta = tmdb_adapter(tmdb_id, season_number, api_key, language)
            except Exception:
                pass

        if meta is None and source == "bangumi":
            raw = top.get("raw_data")
            if raw and isinstance(raw, dict) and raw.get("id"):
                meta = bangumi_adapter(raw, top)
            elif source_id:
                try:
                    detail = bangumi_subject_details(int(source_id))
                    meta = bangumi_adapter(detail, top)
                except Exception:
                    pass

        if meta is None and source == "anilist":
            raw = top.get("raw_data")
            meta = anilist_adapter(raw)

        if meta is None:
            # Fallback: try Bangumi search by candidate name
            search_name = top.get("name") or top.get("original_name") or series_title
            if search_name:
                try:
                    encoded = urllib.parse.quote(search_name)
                    url = f"https://api.bgm.tv/search/subject/{encoded}?type=2&responseGroup=large&max_results=1"
                    req = urllib.request.Request(url, headers={"User-Agent": "myjbrename/1"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    results = data.get("list") if isinstance(data, dict) else []
                    if results:
                        meta = bangumi_adapter(results[0], top)
                except Exception:
                    pass

        # If still no meta, build a minimal one from candidate info
        if meta is None:
            meta = SeriesMetadata(
                title=series_title,
                original_title=top.get("original_name") or "",
                overview=top.get("overview") or "",
                first_air_date=top.get("first_air_date") or "",
                source=source,
                source_id=str(source_id or ""),
            )

        # ── Step 2: Build episode list from directory scan if empty ────
        if not meta.episodes:
            scan = scan_by_path.get(item["path"])
            if scan and hasattr(scan, 'episodes') and scan.episodes:
                for ep in scan.episodes:
                    meta.episodes.append(EpisodeMetadata(
                        season=season_number,
                        episode=ep.index,
                    ))

        # ── Step 3: Write tvshow.nfo ────────────────────────────────────
        meta.year = meta.year or meta.first_air_date[:4]
        tvshow_nfo = generate_tvshow_nfo(meta)
        tvshow_path = series_path / "tvshow.nfo"
        if apply:
            _write_nfo_file(tvshow_path, tvshow_nfo)
        print(f"  NFO tvshow: {tvshow_path}", flush=True)

        # ── Step 4: Write episode NFOs ────────────────────────────────
        episode_nfo_targets: list[tuple[Path, int, int]] = []
        ops = item.get("operations") or []
        if ops:
            seen: set[int] = set()
            for op in ops:
                if op["status"] not in ("rename", "noop"):
                    continue
                target_path = Path(op["target"])
                ep_match = re.search(r"S(\d+)E(\d+)", target_path.name)
                if not ep_match:
                    continue
                ep_season = int(ep_match.group(1))
                ep_index = int(ep_match.group(2))
                if ep_index in seen:
                    continue
                seen.add(ep_index)
                episode_nfo_targets.append((target_path.with_suffix(".nfo"), ep_season, ep_index))
        else:
            seen = set()
            scan = scan_by_path.get(item["path"])
            if scan and hasattr(scan, 'episodes') and scan.episodes:
                for ep in scan.episodes:
                    ep_index = ep.index
                    if ep_index in seen:
                        continue
                    seen.add(ep_index)
                    ep_dir = series_path if ep.episode_dir == "." else series_path / ep.episode_dir
                    if not ep_dir.exists():
                        continue
                    for f in sorted(ep_dir.iterdir()):
                        if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                            if f.suffix.lower() in {".srt", ".ass", ".ssa", ".sub"}:
                                continue
                            episode_nfo_targets.append((f.with_suffix(".nfo"), season_number, ep_index))
                            break
            else:
                try:
                    files = sorted(
                        p for p in series_path.iterdir()
                        if p.is_file() and p.suffix.lower() in {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
                    )
                except OSError:
                    files = []
                for idx, f in enumerate(files, start=1):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    episode_nfo_targets.append((f.with_suffix(".nfo"), season_number, idx))

        # Build episode lookup
        ep_by_num: dict[int, EpisodeMetadata] = {}
        for ep in meta.episodes:
            ep_by_num[ep.episode] = ep

        for nfo_path, ep_season, ep_index in episode_nfo_targets:
            ep_meta = ep_by_num.get(ep_index)
            if ep_meta is None:
                ep_meta = EpisodeMetadata(season=ep_season, episode=ep_index)
            ep_nfo = generate_episode_nfo(ep_meta)
            if apply:
                _write_nfo_file(nfo_path, ep_nfo)
            print(f"  NFO episode: {nfo_path}", flush=True)

        # ── Step 5: Download images ────────────────────────────────────
        if apply:
            download_images(meta, series_path)

        written_count += 1

    return written_count


def _write_nfo_file(path: Path, content: str) -> None:
    """Write an NFO file, creating parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
def main() -> int:
    parser = argparse.ArgumentParser(description="Fresh-scan metadata rename tool for episode-subdirectory media libraries.")
    parser.add_argument("--root", default=".", help="Library root directory")
    parser.add_argument("--path", action="append", default=[], help="Specific series path(s) to process")
    parser.add_argument("--tmdb-api-key", default="", help="TMDB v3 API key or v4 bearer token")
    parser.add_argument("--tmdb-key-file", default="tmdb-api.txt", help="Local file containing TMDB key if --tmdb-api-key is omitted")
    parser.add_argument("--source", choices=["tmdb", "anidb", "both"], default="tmdb", help="Metadata source to use for matching")
    parser.add_argument("--bangumi", action="store_true", default=False, help="Also search Bangumi (bgm.tv) for Chinese titles")
    parser.add_argument("--anilist", action="store_true", default=False, help="Also search AniList (anilist.co) for titles")
    parser.add_argument("--anidb-username", default=os.environ.get("ANIDB_USERNAME", ""), help="AniDB username; defaults to ANIDB_USERNAME")
    parser.add_argument("--anidb-password", default=os.environ.get("ANIDB_PASSWORD", ""), help="AniDB password; defaults to ANIDB_PASSWORD")
    parser.add_argument("--anidb-auth-file", default="anidb-auth.txt", help="Local AniDB credential file if username/password are omitted")
    parser.add_argument("--anidb-client", default="myjbrename", help="AniDB registered UDP API client name")
    parser.add_argument("--anidb-client-version", type=int, default=1, help="AniDB registered UDP API client version")
    parser.add_argument("--anidb-min-interval", type=float, default=4.0, help="Minimum seconds between AniDB UDP requests")
    parser.add_argument("--anidb-timeout", type=float, default=10.0, help="AniDB UDP socket timeout in seconds")
    parser.add_argument("--anidb-retries", type=int, default=1, help="AniDB UDP retry count per command")
    parser.add_argument("--anidb-title-cache", default="anidb-title-cache/anime-titles.xml.gz", help="Local cache path for AniDB anime title dump")
    parser.add_argument("--anidb-title-cache-max-age-hours", type=float, default=24.0, help="Do not re-download AniDB title dump while cache is newer than this")
    parser.add_argument("--no-anidb-title-dump", action="store_true", help="Disable AniDB title dump matching")
    parser.add_argument("--language", default="ja-JP", help="TMDB language for search and season lookups")
    parser.add_argument("--series-title-source", choices=["local", "tmdb", "metadata"], default="tmdb", help="Whether final filenames use local folder name or matched metadata title")
    parser.add_argument("--min-score", type=float, default=0.65, help="Minimum metadata match score to auto-rename")
    parser.add_argument("--allow-fallback", action="store_true", default=False, help="When metadata sources have no match, fall back to using folder name as series title")
    parser.add_argument("--download-metadata", action="store_true", default=False, help="Download TMDB metadata and write NFO files for matched series")
    parser.add_argument("--report", default="tmdb_rename_report.json", help="Where to write run report")
    parser.add_argument("--apply", action="store_true", help="Actually perform renames; default is preview only")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    api_key = args.tmdb_api_key.strip()
    if not api_key:
        key_file = Path(args.tmdb_key_file)
        if not key_file.is_absolute():
            key_file = root / key_file
        if key_file.exists():
            api_key = key_file.read_text(encoding="utf-8").strip()
    if args.source in {"tmdb", "both"} and not api_key:
        raise SystemExit("TMDB key missing. Use --tmdb-api-key or --tmdb-key-file.")
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
    if args.source in {"anidb", "both"} and (not anidb_username or not anidb_password) and args.no_anidb_title_dump:
        raise SystemExit("AniDB credentials missing and title dump disabled. Use credentials or remove --no-anidb-title-dump.")

    scans = [scan_series(path.resolve()) for path in series_paths(root, args.path)]
    print(f"Scanned {len(scans)} series directories", flush=True)
    eligible_idx = 0
    total_eligible = sum(1 for s in scans if s.episode_count)
    for i, scan in enumerate(scans):
        if scan.episode_count:
            eligible_idx += 1
            if args.source in {"tmdb", "both"}:
                print(f"[{eligible_idx}/{total_eligible}] TMDB search: {scan.title_hint[:40]}", flush=True)
                enrich_with_tmdb(scan, api_key, args.language)
    if args.bangumi:
        print("Bangumi lookup...", flush=True)
        enrich_scans_with_bangumi(scans)
    if args.anilist:
        print("AniList lookup...", flush=True)
        enrich_scans_with_anilist(scans)
    if args.source in {"anidb", "both"}:
        if not args.no_anidb_title_dump:
            print("AniDB title dump lookup...", flush=True)
            title_cache = Path(args.anidb_title_cache)
            if not title_cache.is_absolute():
                title_cache = root / title_cache
            enrich_scans_with_anidb_title_dump(scans, title_cache, max_age_hours=args.anidb_title_cache_max_age_hours)
        if anidb_username and anidb_password:
            print("AniDB UDP lookup...", flush=True)
            enrich_scans_with_anidb(
                scans,
                anidb_username,
                anidb_password,
                client_name=args.anidb_client,
                client_version=args.anidb_client_version,
                min_interval=args.anidb_min_interval,
                timeout=args.anidb_timeout,
                retries=args.anidb_retries,
            )
        else:
            for scan in scans:
                scan.reason_flags.append("anidb_udp_credentials_missing")

    print("Evaluating results...", flush=True)
    evaluated = [evaluate_scan(scan, args.min_score, args.series_title_source, allow_fallback=args.allow_fallback) for scan in scans]
    summary = summarize(evaluated)

    # Download metadata and write NFO files if requested
    nfo_count = 0
    if args.download_metadata:
        if args.source not in ("tmdb", "both") or not api_key:
            print("Metadata download requires TMDB source and API key", flush=True)
        else:
            print("Downloading metadata and writing NFO files...", flush=True)
            nfo_count = download_and_write_nfo(evaluated, scans, api_key, args.language, apply=args.apply)
            print(f"NFO files written for {nfo_count} series", flush=True)

    renamed, errors = apply_operations(evaluated, apply=args.apply)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root),
        "apply": args.apply,
        "source": args.source,
        "min_score": args.min_score,
        "series_title_source": args.series_title_source,
        "download_metadata": args.download_metadata,
        "nfo_series_written": nfo_count,
        "summary": summary,
        "renamed_files": renamed,
        "errors": errors,
        "items": evaluated,
    }
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = root / report_path
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote report: {report_path}", flush=True)
    print(f"Summary: rename={summary.get('rename', 0)} noop={summary.get('noop', 0)} skip={summary.get('skip', 0)} unmatched={summary.get('unmatched', 0)} error={summary.get('error', 0)}", flush=True)
    print(f"Files renamed this run: {renamed}", flush=True)
    if nfo_count:
        print(f"NFO files written for {nfo_count} series", flush=True)
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

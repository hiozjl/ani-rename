"""Directory scanning, structure detection, and episode parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from text_utils import (
    clean_text,
    extract_episode_token,
    normalize_for_match,
    strip_series_and_episode_markers,
    _has_kana,
    to_romaji,
    JUNK_PATTERNS,
)

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".srt", ".ass", ".ssa", ".sub"}
EPISODE_DIR_RE = re.compile(r"^(\d{1,3})(?:[\s._-]+(.*))?$")
SEASON_DIR_RE = re.compile(r"^(?:season\s*|s)(\d{1,2})$", re.IGNORECASE)


@dataclass
class EpisodeItem:
    episode_dir: str
    index: int
    local_title_hint: str
    media_files: list[str] = field(default_factory=list)
    proposed_file_names: list[str] = field(default_factory=list)
    season_number: int = 1


@dataclass
class Candidate:
    tmdb_id: int
    name: str
    original_name: str
    first_air_date: str
    original_language: str
    overview: str
    score: float
    reasons: list[str]
    season_fit: dict[str, Any] | None = None
    source: str = "tmdb"
    source_id: str = ""
    zh_title: str = ""
    raw_data: dict[str, Any] | None = None


@dataclass
class SeriesScan:
    path: str
    structure: str
    title_hint: str
    query_variants: list[str]
    fingerprint: dict[str, Any]
    episode_count: int
    episodes: list[EpisodeItem]
    confidence: str
    reason_flags: list[str]
    candidates: list[Candidate]


def title_variants(folder_name: str) -> list[str]:
    variants: list[str] = []
    base = folder_name.strip()
    cleaned = clean_text(base)
    normalized = re.split(r"）|\)|/", base)[0].strip()
    ova_stripped = re.sub(r"^OVA", "", base, flags=re.IGNORECASE).strip()

    jp_via_bracket = re.split(r"[）\)]", base)[0].strip() if "）" in base or ")" in base else ""
    no_tail_chinese = ""
    if _has_kana(base):
        no_tail_chinese = re.sub(
            r"([ヰ-ヶぁ-ん゠-ヿa-zA-Z0-9\s\-_・～~!?！？]+?)"
            r"[一-鿿　-〿]{2,}.*",
            r"\1", base
        ).strip()
        if no_tail_chinese == base:
            no_tail_chinese = ""

    for item in [base, cleaned, normalized, jp_via_bracket, no_tail_chinese]:
        item = item.strip()
        if item and item not in variants:
            variants.append(item)
    if ova_stripped and ova_stripped not in variants:
        variants.append(ova_stripped)

    _PUNCT_RE = re.compile(
        r"[!！?？~～・☆★✩♪♡♥❤️️、。，．,.\-_:;；\[\]【】()（）「」『』\"'＂＇/／|｜#＃@＠&＆*＊+＋=＝]+"
    )
    punct_variants: list[str] = []
    for v in variants:
        no_punct = _PUNCT_RE.sub(" ", v)
        no_punct = re.sub(r"\s+", " ", no_punct).strip()
        if no_punct and no_punct != v and no_punct not in variants:
            punct_variants.append(no_punct)
        for part in _PUNCT_RE.split(v):
            part = part.strip()
            if len(part) >= 2 and part != v and part not in variants:
                punct_variants.append(part)
    for v in punct_variants:
        if v not in variants:
            variants.append(v)

    romaji_variants: list[str] = []
    for v in variants:
        if _has_kana(v):
            romaji = to_romaji(v)
            if romaji and romaji != v and romaji not in variants and romaji not in romaji_variants:
                romaji_variants.append(romaji)
            romaji_clean = re.sub(r"[一-鿿㐀-䶿]", " ", romaji)
            romaji_clean = re.sub(r"\s+", " ", romaji_clean).strip()
            if romaji_clean and romaji_clean != romaji and romaji_clean not in variants and romaji_clean not in romaji_variants:
                romaji_variants.append(romaji_clean)
    variants.extend(romaji_variants)
    return variants


def infer_series_aliases_from_media(media_names: list[str], episode_index: int, title_hint: str) -> list[str]:
    aliases: list[str] = []
    for media in media_names:
        stem = Path(media).stem
        token_match = extract_episode_token(stem)
        candidate = stem
        if token_match:
            candidate = stem[: token_match.start()]
        candidate = clean_text(candidate)
        candidate = re.sub(r"第\s*\d+\s*[話话卷集].*$", "", candidate).strip()
        candidate = re.sub(r"[#＃]\s*\d+.*$", "", candidate).strip()
        candidate = re.sub(r"^OVA", "", candidate, flags=re.IGNORECASE).strip() or candidate
        if candidate and candidate != title_hint and candidate not in aliases:
            aliases.append(candidate)
    return aliases[:3]


def guess_structure(path: Path) -> str:
    child_dirs = [p for p in path.iterdir() if p.is_dir()]
    child_media = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    if child_dirs and any(EPISODE_DIR_RE.match(p.name) for p in child_dirs):
        return "episode_subdirs"
    if child_dirs and any(SEASON_DIR_RE.match(p.name) for p in child_dirs):
        return "season_dirs"
    if child_media and not child_dirs:
        return "flat"
    return "mixed_or_flat"


def list_media_files(path: Path) -> list[str]:
    return sorted([p.name for p in path.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS])


def parse_episode_subdirs(path: Path, series_title: str) -> list[EpisodeItem]:
    episodes: list[EpisodeItem] = []
    for child in sorted([p for p in path.iterdir() if p.is_dir()], key=lambda p: p.name):
        match = EPISODE_DIR_RE.match(child.name)
        if not match:
            continue
        index = int(match.group(1))
        local_title_hint = (match.group(2) or "").strip()
        media_files = list_media_files(child)
        proposals = []
        fallback_title = ""
        if not local_title_hint and media_files:
            title_candidates = []
            for media in media_files:
                candidate = strip_series_and_episode_markers(Path(media).stem, series_title, index)
                if candidate:
                    title_candidates.append(candidate)
            if title_candidates:
                fallback_title = max(title_candidates, key=len)
        if media_files:
            for media in media_files:
                suffix = Path(media).suffix.lower()
                title_part = local_title_hint or fallback_title
                title_part = clean_text(title_part)
                if title_part:
                    proposals.append(f"{series_title} - S01E{index:02d} - {title_part}{suffix}")
                else:
                    proposals.append(f"{series_title} - S01E{index:02d}{suffix}")
        episodes.append(
            EpisodeItem(
                episode_dir=child.name,
                index=index,
                local_title_hint=local_title_hint or fallback_title,
                media_files=media_files,
                proposed_file_names=proposals,
            )
        )
    return episodes


def parse_flat_media(path: Path, series_title: str) -> list[EpisodeItem]:
    media_files = sorted(
        [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS],
        key=lambda p: p.name,
    )
    video_files = [p for p in media_files if p.suffix.lower() not in {".srt", ".ass", ".ssa", ".sub"}]
    if not video_files:
        return []
    episodes: list[EpisodeItem] = []
    for index, file in enumerate(video_files, start=1):
        stem = Path(file).stem
        hint = strip_series_and_episode_markers(stem, series_title, index)
        hint = clean_text(hint)
        if not hint:
            hint = ""
        dir_files = [file.name]
        for f in media_files:
            if f == file:
                continue
            if f.suffix.lower() in {".srt", ".ass", ".ssa", ".sub"}:
                f_stem = Path(f).stem
                if stem[:max(4, len(stem)//2)] in f_stem or f_stem[:max(4, len(f_stem)//2)] in stem:
                    dir_files.append(f.name)
        suffix = file.suffix.lower()
        title_part = hint
        title_part = clean_text(title_part)
        if title_part:
            proposal = f"{series_title} - S01E{index:02d} - {title_part}{suffix}"
        else:
            proposal = f"{series_title} - S01E{index:02d}{suffix}"
        episodes.append(
            EpisodeItem(
                episode_dir=".",
                index=index,
                local_title_hint=hint,
                media_files=sorted(dir_files),
                proposed_file_names=[proposal],
            )
        )
    return episodes


def fingerprint(path: Path, episodes: list[EpisodeItem]) -> dict[str, Any]:
    return {
        "path": str(path),
        "entry_count": len(list(path.iterdir())),
        "episode_dirs": [ep.episode_dir for ep in episodes],
        "episode_numbers": [ep.index for ep in episodes],
        "media_file_count": sum(len(ep.media_files) for ep in episodes),
    }


def parse_season_dirs(path: Path, series_title: str) -> list[EpisodeItem]:
    child_dirs = sorted(
        [p for p in path.iterdir() if p.is_dir() and SEASON_DIR_RE.match(p.name)],
        key=lambda p: int(SEASON_DIR_RE.match(p.name).group(1)),
    )
    episodes: list[EpisodeItem] = []
    global_index = 1
    for season_dir in child_dirs:
        season_match = SEASON_DIR_RE.match(season_dir.name)
        season_num = int(season_match.group(1))
        media_files = sorted(
            [p for p in season_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS],
            key=lambda p: p.name,
        )
        video_files = [p for p in media_files if p.suffix.lower() not in {".srt", ".ass", ".ssa", ".sub"}]
        if not video_files:
            continue
        for index, file in enumerate(video_files, start=1):
            stem = Path(file).stem
            hint = strip_series_and_episode_markers(stem, series_title, global_index)
            hint = clean_text(hint)
            if not hint:
                hint = ""
            dir_files = [file.name]
            for f in media_files:
                if f != file and f.suffix.lower() in {".srt", ".ass", ".ssa", ".sub"}:
                    if Path(f).stem.startswith(stem[:max(4, len(stem)//2)]):
                        dir_files.append(f.name)
            suffix = file.suffix.lower()
            title_part = hint
            title_part = clean_text(title_part)
            if title_part:
                proposal = f"{series_title} - S{season_num:02d}E{index:02d} - {title_part}{suffix}"
            else:
                proposal = f"{series_title} - S{season_num:02d}E{index:02d}{suffix}"
            episodes.append(
                EpisodeItem(
                    episode_dir=season_dir.name,
                    index=global_index,
                    local_title_hint=hint,
                    media_files=sorted(dir_files),
                    proposed_file_names=[proposal],
                    season_number=season_num,
                )
            )
            global_index += 1
    return episodes


def scan_series(path: Path) -> SeriesScan:
    structure = guess_structure(path)
    title_hint = path.name.strip()
    queries = title_variants(title_hint)
    episodes: list[EpisodeItem] = []
    reason_flags: list[str] = []
    confidence = "low"
    if structure == "episode_subdirs":
        episodes = parse_episode_subdirs(path, title_hint)
        if episodes:
            confidence = "medium"
        else:
            reason_flags.append("no_episode_media_found")
    elif structure == "flat":
        episodes = parse_flat_media(path, title_hint)
        if episodes:
            confidence = "medium"
        else:
            reason_flags.append("no_flat_media_found")
    elif structure == "season_dirs":
        episodes = parse_season_dirs(path, title_hint)
        if episodes:
            confidence = "medium"
        else:
            reason_flags.append("no_season_media_found")
    else:
        reason_flags.append(f"unsupported_structure:{structure}")

    if title_hint.upper().startswith("OVA"):
        reason_flags.append("ova_prefix_detected")
    if any("special" in q.lower() for q in queries):
        reason_flags.append("special_marker_detected")

    media_aliases: list[str] = []
    for ep in episodes[:3]:
        media_aliases.extend(infer_series_aliases_from_media(ep.media_files, ep.index, title_hint))
    for alias in media_aliases:
        alias = alias.strip()
        if alias and alias not in queries:
            queries.append(alias)

    return SeriesScan(
        path=str(path),
        structure=structure,
        title_hint=title_hint,
        query_variants=queries,
        fingerprint=fingerprint(path, episodes),
        episode_count=len(episodes),
        episodes=episodes,
        confidence=confidence,
        reason_flags=reason_flags,
        candidates=[],
    )

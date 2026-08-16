"""Directory scanning, structure detection, and episode parsing."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
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

MEDIA_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts",
    ".mov", ".mpg", ".mpeg", ".wmv", ".flv", ".webm",
    ".srt", ".ass", ".ssa", ".sub",
}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub"}
VIDEO_EXTS = MEDIA_EXTS - SUBTITLE_EXTS
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


def tvshow_nfo_titles(path: Path) -> list[str]:
    """Return series titles persisted in ``tvshow.nfo``, if any.

    These are used before other search variants so a folder that was renamed
    after a previous metadata match keeps resolving to the same series.
    """
    nfo = path / "tvshow.nfo"
    if not nfo.is_file():
        return []
    try:
        root = ET.parse(str(nfo)).getroot()
    except Exception:
        return []
    titles: list[str] = []
    for field in ("originaltitle", "title"):
        value = (root.findtext(field) or "").strip()
        if value and value not in titles:
            titles.append(value)
    return titles


def infer_series_aliases_from_media(media_names: list[str], episode_index: int, title_hint: str) -> list[str]:
    aliases: list[str] = []
    for media in media_names:
        stem = Path(media).stem
        token_match = extract_episode_token(stem)
        # Only trust filenames that contain an explicit SxxExx token.  After a
        # previous rename these files have the canonical form
        # "Series - S01E01 - Title.mkv", and the part before SxxExx is the
        # series name that was used before.
        if not token_match:
            continue
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


def stable_entry_name_hash(path: Path) -> str:
    """Deterministic hash of descendant names, used to detect file renames.

    Python's built-in ``hash()`` is randomized per process, so it is not
    suitable for fingerprints that are persisted between runs.  Relative
    paths are used so renaming the series folder itself does not change the
    digest, while renaming files inside any episode/season subdirectory does.
    """
    try:
        names = sorted(str(p.relative_to(path)) for p in path.rglob("*"))
    except OSError:
        return ""
    payload = "\n".join(names).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def media_identity_key(path: Path) -> str:
    """Return a stable identity for one media file.

    ``(st_dev, st_ino, st_size, st_mtime_ns)`` is unchanged by renaming the
    file or the parent directory.  Hard links to the same content therefore
    share a key, while an in-place content modification produces a new key.
    """
    st = path.stat()
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def series_content_identity(path: Path) -> str:
    """Return a stable, content-based identity for a series folder.

    The identity is built from the sorted multiset of video-file
    ``(device, inode, size, mtime)`` tuples below ``path``.  Subtitles are
    intentionally ignored so adding/removing a subtitle does not invalidate a
    previous metadata match.  Renaming files/folders does not change the
    identity, and two folders made of hard links to the same videos produce
    the same value.  Returns ``""`` when no videos can be inspected.
    """
    keys: list[str] = []
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return ""
    for entry in entries:
        try:
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
                keys.append(media_identity_key(entry))
        except OSError:
            continue
    if not keys:
        return ""
    payload = "\0".join(sorted(keys)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def dedupe_series_folders(paths: list[Path], prefer_nfo: bool = False) -> list[Path]:
    """Remove folders that are hard-link duplicates of an earlier folder.

    Only folders with a usable video-content identity are deduplicated;
    folders whose content cannot be fingerprinted are kept untouched.  When
    ``prefer_nfo`` is true, the representative of each duplicate group prefers
    ``tvshow.nfo`` and the project's native episode-subdirectory layout over
    an Emby-style ``Season N`` export.
    """
    identities: dict[Path, str] = {}
    groups: dict[str, list[Path]] = {}
    for path in paths:
        try:
            identity = series_content_identity(path)
        except OSError:
            identity = ""
        identities[path] = identity
        if identity:
            groups.setdefault(identity, []).append(path)

    if not prefer_nfo:
        seen: set[str] = set()
        result: list[Path] = []
        for path in paths:
            identity = identities[path]
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            result.append(path)
        return result

    selected: set[Path] = {path for path, identity in identities.items() if not identity}

    def representative_rank(path: Path) -> tuple[int, int, str]:
        try:
            has_nfo = (path / "tvshow.nfo").is_file()
            structure = guess_structure(path)
        except OSError:
            return (1, 3, path.name.lower())
        structure_rank = {
            "episode_subdirs": 0,
            "flat": 1,
            "season_dirs": 2,
        }.get(structure, 3)
        return (0 if has_nfo else 1, structure_rank, path.name.lower())

    for group in groups.values():
        if len(group) == 1:
            selected.add(group[0])
            continue
        representative = min(group, key=representative_rank)
        selected.add(representative)
    return [path for path in paths if path in selected]


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


def fingerprint(path: Path, episodes: list[EpisodeItem], structure: str = "") -> dict[str, Any]:
    try:
        entry_count = len(list(path.iterdir()))
    except OSError:
        entry_count = 0
    if not structure:
        try:
            structure = guess_structure(path)
        except OSError:
            structure = ""
    return {
        "path": str(path),
        "structure": structure,
        "entry_count": entry_count,
        "episode_count": len(episodes),
        "episode_dirs": [ep.episode_dir for ep in episodes],
        "episode_numbers": [ep.index for ep in episodes],
        "media_file_count": sum(len(ep.media_files) for ep in episodes),
        "name_hash": stable_entry_name_hash(path),
        "content_identity": series_content_identity(path),
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
    # Prefer names recovered from already-canonical media files.  The folder
    # may still have its pre-rename name, but the file prefix is the title
    # that was actually applied previously.
    existing_lower = {q.lower() for q in queries}
    for alias in reversed(media_aliases):
        alias = alias.strip()
        if alias and alias.lower() not in existing_lower:
            queries.insert(0, alias)
            existing_lower.add(alias.lower())

    # A previously downloaded tvshow.nfo is the strongest local hint about
    # which metadata title was selected before.
    for nfo_title in reversed(tvshow_nfo_titles(path)):
        if nfo_title and nfo_title.lower() not in existing_lower:
            queries.insert(0, nfo_title)
            existing_lower.add(nfo_title.lower())

    # Truncate excessive search variants — more than 8 rarely helps
    seen = set()
    deduped = []
    for q in queries:
        low = q.lower()
        if low not in seen:
            seen.add(low)
            deduped.append(q)
    queries = deduped[:8]

    return SeriesScan(
        path=str(path),
        structure=structure,
        title_hint=title_hint,
        query_variants=queries,
        fingerprint=fingerprint(path, episodes, structure),
        episode_count=len(episodes),
        episodes=episodes,
        confidence=confidence,
        reason_flags=reason_flags,
        candidates=[],
    )

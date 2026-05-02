"""AniDB UDP API — anime lookup, episode titles, title dump matching."""

from __future__ import annotations

import gzip
import json
import re
import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from scanner import Candidate, SeriesScan
from text_utils import fuzzy_match_score, normalize_for_match
from scoring import BONUS_STRUCTURE, SCORE_THRESHOLD


ANIDB_TITLES_URL = "http://anidb.net/api/anime-titles.xml.gz"
XML_NS = {"xml": "http://www.w3.org/XML/1998/namespace"}


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def anidb_escape(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _unescape_anidb_field(value: str) -> str:
    return value.replace("<br />", "\n").replace("`", "'")


def _parse_anidb_anime_line(line: str) -> dict[str, Any]:
    fields = [_unescape_anidb_field(item) for item in line.split("|")]
    fields.extend([""] * max(0, 19 - len(fields)))
    return {
        "aid": _parse_int(fields[0]),
        "eps": _parse_int(fields[1]),
        "normal_episode_count": _parse_int(fields[2]),
        "special_count": _parse_int(fields[3]),
        "rating": _parse_int(fields[4]),
        "year": fields[10],
        "type": fields[11],
        "romaji": fields[12],
        "kanji": fields[13],
        "english": fields[14],
        "other": fields[15],
        "short_names": fields[16],
        "synonyms": fields[17],
        "categories": fields[18],
    }


class AniDBUdpClient:
    def __init__(
        self,
        username: str,
        password: str,
        client_name: str = "myjbrename",
        client_version: int = 1,
        host: str = "api.anidb.net",
        port: int = 9000,
        timeout: float = 10.0,
        min_interval: float = 4.0,
        retries: int = 1,
    ) -> None:
        self.username = username
        self.password = password
        self.client_name = client_name
        self.client_version = client_version
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self.session_key = ""
        self._last_request_at = 0.0
        old_default = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        finally:
            socket.setdefaulttimeout(old_default)
        self.host = addrs[0][4][0]
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(timeout)

    def close(self) -> None:
        try:
            if self.session_key:
                self.command("LOGOUT")
        finally:
            self.session_key = ""
            self._sock.close()

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def command(self, command: str, include_session: bool = True) -> tuple[int, str, list[str]]:
        if include_session and self.session_key and "&s=" not in command and not command.endswith(f"s={self.session_key}"):
            command = f"{command}&s={self.session_key}" if "&" in command or " " in command else f"{command} s={self.session_key}"
        last_timeout: TimeoutError | None = None
        raw = b""
        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit()
            self._sock.sendto(command.encode("utf-8"), (self.host, self.port))
            self._last_request_at = time.monotonic()
            try:
                raw, _ = self._sock.recvfrom(4096)
                break
            except TimeoutError as exc:
                last_timeout = exc
                if attempt >= self.retries:
                    raise
        if not raw and last_timeout:
            raise last_timeout
        text = raw.decode("utf-8", errors="replace")
        lines = text.rstrip("\n").split("\n")
        header = lines[0] if lines else ""
        match = re.match(r"^(\d{3})(?:\s+(.*))?$", header)
        if not match:
            raise RuntimeError(f"Unexpected AniDB UDP response code: {header[:3]!r}")
        return int(match.group(1)), match.group(2) or "", lines[1:]

    def auth(self) -> None:
        command = (
            "AUTH "
            f"user={anidb_escape(self.username)}"
            f"&pass={anidb_escape(self.password)}"
            "&protover=3"
            f"&client={anidb_escape(self.client_name)}"
            f"&clientver={self.client_version}"
            "&enc=UTF-8"
        )
        code, message, _ = self.command(command, include_session=False)
        if code not in {200, 201}:
            raise RuntimeError(f"AniDB auth failed: {code} {message}")
        parts = message.split()
        if not parts:
            raise RuntimeError(f"AniDB auth did not return a session key: {code} {message}")
        self.session_key = parts[0]

    def anime_by_name(self, name: str) -> dict[str, Any] | None:
        code, message, lines = self.command(f"ANIME aname={anidb_escape(name)}")
        if code == 230 and lines:
            return _parse_anidb_anime_line(lines[0])
        if code == 330:
            return None
        raise RuntimeError(f"AniDB ANIME lookup failed for {name!r}: {code} {message}")

    def episodes_by_aid(self, aid: int) -> list[dict[str, Any]]:
        code, message, lines = self.command(f"EPISODE aid={aid}")
        if code in (230,):
            return []
        if code != 200 or not lines:
            raise RuntimeError(f"AniDB EPISODE lookup failed for aid={aid}: {code} {message}")
        episodes: list[dict[str, Any]] = []
        for line in lines:
            fields = [_unescape_anidb_field(item) for item in line.split("|")]
            if len(fields) < 4:
                continue
            ep_no_str = fields[1].strip("'")
            try:
                ep_no = int(ep_no_str)
            except ValueError:
                ep_no = 0
            episodes.append({
                "episode_number": ep_no,
                "title_english": fields[2].strip("'") if len(fields) > 2 else "",
                "title_romaji": fields[3].strip("'") if len(fields) > 3 else "",
            })
        return episodes


def score_anidb_candidate(scan: SeriesScan, result: dict[str, Any]) -> Candidate:
    names = [
        result.get("romaji") or "",
        result.get("kanji") or "",
        result.get("english") or "",
        result.get("other") or "",
    ]
    for field_name in ("short_names", "synonyms"):
        names.extend([x for x in (result.get(field_name) or "").split("'") if x])

    title_score = 0.0
    title_norms = [normalize_for_match(x) for x in scan.query_variants if x.strip()]
    candidate_norms = [normalize_for_match(x) for x in names if x.strip()]
    for left in title_norms:
        for right in candidate_norms:
            if left and right:
                title_score = max(title_score, fuzzy_match_score(left, right))

    score = title_score * 0.55
    reason_flags = ["anidb_exact_title_lookup"]
    normal_count = _parse_int(result.get("normal_episode_count"))
    total_count = _parse_int(result.get("eps"))
    compare_count = normal_count or total_count
    season_fit = None
    if scan.episode_count and compare_count:
        count_fit = 1.0 if compare_count == scan.episode_count else max(
            0.0,
            1 - abs(compare_count - scan.episode_count) / max(compare_count, scan.episode_count, 1),
        )
        score += count_fit * 0.3
        season_fit = {
            "source": "anidb",
            "anidb_episode_count": total_count,
            "anidb_normal_episode_count": normal_count,
            "anidb_special_count": _parse_int(result.get("special_count")),
            "local_episode_count": scan.episode_count,
        }
        reason_flags.append("episode_count_match" if compare_count == scan.episode_count else "episode_count_mismatch")
    else:
        reason_flags.append("no_episode_count_data")

    if scan.structure in {"episode_subdirs", "season_dirs"}:
        score += BONUS_STRUCTURE
        reason_flags.append("structure_detected")

    display_name = result.get("kanji") or result.get("romaji") or result.get("english") or result.get("other") or ""
    original_name = result.get("romaji") or display_name
    aid = _parse_int(result.get("aid"))
    return Candidate(
        tmdb_id=aid,
        name=display_name,
        original_name=original_name,
        first_air_date=str(result.get("year") or ""),
        original_language="ja",
        overview=f"AniDB type: {result.get('type') or ''}".strip(),
        score=round(max(0.0, min(score, 0.99)), 3),
        reasons=reason_flags,
        season_fit=season_fit,
        source="anidb",
        source_id=str(aid),
    )


def enrich_with_anidb_client(scan: SeriesScan, client: AniDBUdpClient) -> None:
    candidates: dict[int, Candidate] = {}
    for query in scan.query_variants:
        if not query:
            continue
        try:
            result = client.anime_by_name(query)
        except Exception as exc:
            scan.reason_flags.append(f"anidb_lookup_failed:{query}:{type(exc).__name__}")
            continue
        if not result:
            continue
        candidate = score_anidb_candidate(scan, result)
        current = candidates.get(candidate.tmdb_id)
        if current is None or candidate.score > current.score:
            candidates[candidate.tmdb_id] = candidate
    scan.candidates = sorted([*scan.candidates, *candidates.values()], key=lambda item: item.score, reverse=True)[:5]


def enrich_with_anidb(
    scan: SeriesScan,
    username: str,
    password: str,
    client_name: str = "myjbrename",
    client_version: int = 1,
    min_interval: float = 4.0,
    timeout: float = 10.0,
    retries: int = 1,
) -> None:
    client = AniDBUdpClient(
        username=username,
        password=password,
        client_name=client_name,
        client_version=client_version,
        min_interval=min_interval,
        timeout=timeout,
        retries=retries,
    )
    try:
        try:
            client.auth()
        except Exception as exc:
            scan.reason_flags.append(f"anidb_auth_failed:{type(exc).__name__}")
            return
        enrich_with_anidb_client(scan, client)
    finally:
        client.close()


def enrich_scans_with_anidb(
    scans: list[SeriesScan],
    username: str,
    password: str,
    client_name: str = "myjbrename",
    client_version: int = 1,
    min_interval: float = 4.0,
    timeout: float = 10.0,
    retries: int = 1,
) -> None:
    eligible = [scan for scan in scans if scan.structure in {"episode_subdirs", "season_dirs"} and scan.episode_count]
    if not eligible:
        return
    client = AniDBUdpClient(
        username=username,
        password=password,
        client_name=client_name,
        client_version=client_version,
        min_interval=min_interval,
        timeout=timeout,
        retries=retries,
    )
    try:
        try:
            client.auth()
        except Exception as exc:
            for scan in eligible:
                scan.reason_flags.append(f"anidb_auth_failed:{type(exc).__name__}")
            return
        total = len(eligible)
        for i, scan in enumerate(eligible):
            print(f"[{i+1}/{total}] AniDB UDP: {scan.title_hint[:40]}", flush=True)
            enrich_with_anidb_client(scan, client)
    finally:
        client.close()


# ── AniDB title dump ─────────────────────────────────────────────────


def ensure_anidb_title_dump(cache_path: Path, max_age_hours: float = 24.0, url: str = ANIDB_TITLES_URL) -> Path | None:
    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours <= max_age_hours:
            return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "myjbrename/1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            cache_path.write_bytes(response.read())
        return cache_path
    except Exception:
        return cache_path if cache_path.exists() else None


def iter_anidb_title_dump(cache_path: Path):
    with gzip.open(cache_path, "rb") as handle:
        for _event, anime in ET.iterparse(handle, events=("end",)):
            if anime.tag != "anime":
                continue
            aid = _parse_int(anime.attrib.get("aid", ""))
            titles: list[dict[str, str]] = []
            for title in anime.findall("title"):
                text = (title.text or "").strip()
                if not text:
                    continue
                titles.append({
                    "title": text,
                    "type": title.attrib.get("type", ""),
                    "lang": title.attrib.get(f"{{{XML_NS['xml']}}}lang", ""),
                })
            if aid and titles:
                yield aid, titles
            anime.clear()


def _anidb_title_weight(title_type: str, lang: str) -> float:
    weight = {
        "main": 0.04,
        "official": 0.03,
        "syn": 0.015,
        "short": -0.02,
    }.get(title_type, 0.0)
    if lang in {"ja", "x-jat", "en", "zh-Hans", "zh-Hant"}:
        weight += 0.01
    return weight


def _best_anidb_title_for_display(titles: list[dict[str, str]]) -> str:
    preferred = sorted(
        titles,
        key=lambda item: (
            item.get("type") != "official",
            item.get("lang") not in {"ja", "x-jat", "en"},
            len(item.get("title", "")),
        ),
    )
    return preferred[0]["title"] if preferred else ""


def score_anidb_title_dump_candidate(scan: SeriesScan, aid: int, titles: list[dict[str, str]]) -> Candidate | None:
    title_norms = [normalize_for_match(x) for x in scan.query_variants if x.strip()]
    best_score = 0.0
    best_title = ""
    best_meta: dict[str, str] = {}
    zh_type_rank = {"official": 0, "main": 1, "syn": 2}
    best_zh_title = ""
    best_zh_rank = 99
    for title_item in titles:
        title = title_item["title"]
        right = normalize_for_match(title)
        if not right:
            continue
        for left in title_norms:
            if not left:
                continue
            ratio = fuzzy_match_score(left, right)
            ratio = min(0.99, ratio + _anidb_title_weight(title_item.get("type", ""), title_item.get("lang", "")))
            if ratio > best_score:
                best_score = ratio
                best_title = title
                best_meta = title_item
        lang = title_item.get("lang", "")
        if lang in {"zh-Hans", "zh-Hant", "zh"} and title.strip():
            rank = zh_type_rank.get(title_item.get("type", ""), 3)
            if rank < best_zh_rank or (rank == best_zh_rank and len(title) > len(best_zh_title)):
                best_zh_rank = rank
                best_zh_title = title.strip()
    if best_score < SCORE_THRESHOLD:
        return None

    display_name = _best_anidb_title_for_display(titles) or best_title
    return Candidate(
        tmdb_id=aid,
        name=display_name,
        original_name=best_title,
        first_air_date="",
        original_language=best_meta.get("lang", ""),
        overview="AniDB title dump match",
        score=round(max(0.0, min(best_score, 0.99)), 3),
        reasons=[
            "anidb_title_dump_match",
            f"title_type:{best_meta.get('type', '')}",
            f"title_lang:{best_meta.get('lang', '')}",
        ],
        season_fit={"source": "anidb_title_dump", "aid": aid, "matched_title": best_title},
        source="anidb",
        source_id=str(aid),
        zh_title=best_zh_title,
    )


def enrich_scans_with_anidb_title_dump(scans: list[SeriesScan], cache_path: Path, max_age_hours: float = 24.0) -> None:
    eligible = [scan for scan in scans if scan.structure in {"episode_subdirs", "season_dirs"} and scan.episode_count]
    if not eligible:
        return
    dump_path = ensure_anidb_title_dump(cache_path, max_age_hours=max_age_hours)
    if not dump_path:
        for scan in eligible:
            scan.reason_flags.append("anidb_title_dump_unavailable")
        return
    try:
        per_scan: list[dict[int, Candidate]] = [dict() for _ in eligible]
        for aid, titles in iter_anidb_title_dump(dump_path):
            for index, scan in enumerate(eligible):
                candidate = score_anidb_title_dump_candidate(scan, aid, titles)
                if not candidate:
                    continue
                current = per_scan[index].get(candidate.tmdb_id)
                if current is None or candidate.score > current.score:
                    per_scan[index][candidate.tmdb_id] = candidate
        for scan, candidates in zip(eligible, per_scan):
            scan.candidates = sorted([*scan.candidates, *candidates.values()], key=lambda item: item.score, reverse=True)[:5]
            if candidates:
                scan.reason_flags.append("anidb_title_dump_used")
    except Exception as exc:
        for scan in eligible:
            scan.reason_flags.append(f"anidb_title_dump_failed:{type(exc).__name__}")


def read_anidb_auth_file(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return "", ""
    if text.lstrip().startswith("{"):
        data = json.loads(text)
        return str(data.get("username", "")).strip(), str(data.get("password", "")).strip()

    values: dict[str, str] = {}
    plain_lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
        else:
            plain_lines.append(line)
    if values:
        return values.get("username", ""), values.get("password", "")
    if len(plain_lines) >= 2:
        return plain_lines[0], plain_lines[1]
    return "", ""

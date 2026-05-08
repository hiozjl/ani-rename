"""Bangumi (bgm.tv) API — search, subject details, candidate scoring."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from scanner import Candidate, SeriesScan
from text_utils import fuzzy_match_score, normalize_for_match
from scoring import BONUS_STRUCTURE, SCORE_THRESHOLD


BANGUMI_SEARCH_URL = "https://api.bgm.tv/search/subject/{}?type=2&responseGroup=large&max_results=3"
BANGUMI_SUBJECT_URL = "https://api.bgm.tv/subject/{}?responseGroup=large"


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bangumi_search(keyword: str) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(keyword)
    url = BANGUMI_SEARCH_URL.format(encoded)
    req = urllib.request.Request(url, headers={"User-Agent": "myjbrename/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    if isinstance(data, dict) and "list" in data:
        return data["list"]
    return []


def score_bangumi_candidate(scan: SeriesScan, result: dict[str, Any]) -> Candidate | None:
    bangumi_id = _parse_int(result.get("id"))
    if not bangumi_id:
        return None

    names = []
    if result.get("name"):
        names.append(result["name"])
    if result.get("name_cn"):
        names.append(result["name_cn"])

    title_norms = [normalize_for_match(x) for x in scan.query_variants if x.strip()]
    candidate_norms = [normalize_for_match(x) for x in names if x.strip()]
    if not candidate_norms:
        return None

    title_score = 0.0
    for left in title_norms:
        for right in candidate_norms:
            if left and right:
                title_score = max(title_score, fuzzy_match_score(left, right))

    score = title_score * 0.55
    reason_flags = ["bangumi_match"]

    eps_count = _parse_int(result.get("eps_count")) or _parse_int(result.get("eps"))
    if scan.episode_count and eps_count:
        count_fit = 1.0 if eps_count == scan.episode_count else max(
            0.0,
            1 - abs(eps_count - scan.episode_count) / max(eps_count, scan.episode_count, 1),
        )
        score += count_fit * 0.3
        season_fit = {
            "source": "bangumi",
            "bangumi_id": bangumi_id,
            "bangumi_episode_count": eps_count,
            "local_episode_count": scan.episode_count,
        }
        reason_flags.append(
            "episode_count_match" if eps_count == scan.episode_count else "episode_count_mismatch"
        )
    else:
        season_fit = {"source": "bangumi", "bangumi_id": bangumi_id}
        reason_flags.append("no_episode_count_data")

    if scan.structure in {"episode_subdirs", "season_dirs"}:
        score += BONUS_STRUCTURE
        reason_flags.append("structure_detected")

    display_name = result.get("name_cn") or result.get("name") or ""
    original_name = result.get("name") or ""
    zh_title = result.get("name_cn") or ""

    if score < SCORE_THRESHOLD:
        return None

    return Candidate(
        tmdb_id=bangumi_id,
        name=display_name,
        original_name=original_name,
        first_air_date=str(result.get("air_date") or ""),
        original_language="ja",
        overview=f"Bangumi rating: {result.get('rating', {}).get('score', 'N/A')}",
        score=round(max(0.0, min(score, 0.99)), 3),
        reasons=reason_flags,
        season_fit=season_fit,
        source="bangumi",
        source_id=str(bangumi_id),
        zh_title=zh_title,
        raw_data=result,
    )


def enrich_with_bangumi(scan: SeriesScan) -> None:
    candidates: dict[int, Candidate] = {}
    for query in scan.query_variants:
        if not query:
            continue
        try:
            results = _bangumi_search(query)
        except Exception as exc:
            scan.reason_flags.append(f"bangumi_search_failed:{type(exc).__name__}")
            continue
        for result in results:
            candidate = score_bangumi_candidate(scan, result)
            if candidate is None:
                continue
            key = (candidate.source, candidate.source_id)
            current = candidates.get(key)
            if current is None or candidate.score > current.score:
                candidates[key] = candidate
    scan.candidates = sorted(
        [*scan.candidates, *candidates.values()],
        key=lambda item: item.score, reverse=True,
    )[:5]


def enrich_scans_with_bangumi(scans: list[SeriesScan]) -> None:
    eligible = [scan for scan in scans if scan.structure in {"episode_subdirs", "season_dirs"} and scan.episode_count]
    if not eligible:
        return
    total = len(eligible)
    for i, scan in enumerate(eligible):
        print(f"[{i+1}/{total}] Bangumi search: {scan.title_hint[:40]}", flush=True)
        enrich_with_bangumi(scan)


def bangumi_subject_details(subject_id: int) -> dict[str, Any] | None:
    url = BANGUMI_SUBJECT_URL.format(subject_id)
    req = urllib.request.Request(url, headers={"User-Agent": "myjbrename/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def bangumi_episodes_to_map(subject_data: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    ep_map: dict[int, dict[str, Any]] = {}
    if not subject_data:
        return ep_map
    eps = subject_data.get("eps")
    if isinstance(eps, list):
        for ep in eps:
            ep_num = ep.get("ep")
            if ep_num and isinstance(ep_num, int):
                ep_map[ep_num] = ep
    return ep_map

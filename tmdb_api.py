"""TMDB API v3 — search, series/season details, candidate scoring."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from scanner import Candidate, SeriesScan
from text_utils import fuzzy_match_score, normalize_for_match
from scoring import (
    WEIGHT_TITLE, WEIGHT_EPISODE_COUNT, WEIGHT_CONTINUITY,
    BONUS_STRUCTURE, PENALTY_OVA,
)


def _fetch_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _tmdb_request(url: str, api_key: str) -> Any:
    import re
    api_key = api_key.strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", api_key):
        sep = '&' if '?' in url else '?'
        return _fetch_json(f"{url}{sep}api_key={urllib.parse.quote(api_key)}", {"accept": "application/json"})
    headers = {"Authorization": f"Bearer {api_key}", "accept": "application/json"}
    return _fetch_json(url, headers)


def search_tmdb(query: str, api_key: str, language: str) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"query": query, "language": language, "include_adult": "true", "page": 1}
    )
    url = f"https://api.themoviedb.org/3/search/tv?{params}"
    payload = _tmdb_request(url, api_key)
    return payload.get("results", [])


def tmdb_season_details(series_id: int, season_number: int, api_key: str, language: str) -> dict[str, Any] | None:
    url = f"https://api.themoviedb.org/3/tv/{series_id}/season/{season_number}?language={urllib.parse.quote(language)}"
    try:
        return _tmdb_request(url, api_key)
    except Exception:
        return None


def tmdb_series_details(series_id: int, api_key: str, language: str) -> dict[str, Any] | None:
    url = f"https://api.themoviedb.org/3/tv/{series_id}?language={urllib.parse.quote(language)}&append_to_response=credits,images"
    try:
        return _tmdb_request(url, api_key)
    except Exception:
        return None


def score_candidate(scan: SeriesScan, result: dict[str, Any], season_data: dict[str, Any] | None) -> Candidate:
    title_score = 0.0
    reason_flags: list[str] = []
    names = [result.get("name") or "", result.get("original_name") or ""]
    title_norms = [normalize_for_match(x) for x in scan.query_variants if x.strip()]
    candidate_norms = [normalize_for_match(x) for x in names if x.strip()]
    for left in title_norms:
        for right in candidate_norms:
            if left and right:
                title_score = max(title_score, fuzzy_match_score(left, right))
    score = title_score * WEIGHT_TITLE

    season_fit = None
    if season_data and scan.episode_count:
        tmdb_eps = [ep for ep in season_data.get("episodes", []) if isinstance(ep.get("episode_number"), int)]
        tmdb_count = len(tmdb_eps)
        contiguous = [ep.index for ep in scan.episodes] == list(range(1, scan.episode_count + 1))
        count_fit = 1.0 if tmdb_count == scan.episode_count else max(0.0, 1 - abs(tmdb_count - scan.episode_count) / max(tmdb_count, scan.episode_count, 1))
        continuity_fit = 1.0 if contiguous else 0.3
        score += count_fit * WEIGHT_EPISODE_COUNT + continuity_fit * WEIGHT_CONTINUITY
        season_fit = {
            "season_number": season_data.get("season_number"),
            "tmdb_episode_count": tmdb_count,
            "local_episode_count": scan.episode_count,
            "contiguous_local_numbering": contiguous,
        }
        if tmdb_count == scan.episode_count:
            reason_flags.append("episode_count_match")
        else:
            reason_flags.append("episode_count_mismatch")
    else:
        reason_flags.append("no_season_fit_data")

    if scan.structure in {"episode_subdirs", "season_dirs"}:
        score += BONUS_STRUCTURE
        reason_flags.append("structure_detected")
    if scan.title_hint.upper().startswith("OVA"):
        score -= PENALTY_OVA
        reason_flags.append("ova_requires_manual_review")

    return Candidate(
        tmdb_id=result["id"],
        name=result.get("name") or "",
        original_name=result.get("original_name") or "",
        first_air_date=result.get("first_air_date") or "",
        original_language=result.get("original_language") or "",
        overview=result.get("overview") or "",
        score=round(max(0.0, min(score, 0.99)), 3),
        reasons=reason_flags,
        season_fit=season_fit,
        source="tmdb",
        source_id=str(result["id"]),
    )


def enrich_with_tmdb(scan: SeriesScan, api_key: str, language: str) -> None:
    candidates: dict[int, Candidate] = {}
    for query in scan.query_variants:
        if not query:
            continue
        try:
            results = search_tmdb(query, api_key, language)
        except Exception as exc:
            scan.reason_flags.append(f"tmdb_search_failed:{query}:{type(exc).__name__}")
            continue
        for result in results[:5]:
            season_data = tmdb_season_details(result["id"], 1, api_key, language) if scan.episode_count else None
            candidate = score_candidate(scan, result, season_data)
            key = (candidate.source, candidate.source_id)
            current = candidates.get(key)
            if current is None or candidate.score > current.score:
                candidates[key] = candidate
    scan.candidates = sorted(candidates.values(), key=lambda item: item.score, reverse=True)[:5]

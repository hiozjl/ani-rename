"""AniList GraphQL API — search, candidate scoring."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from scanner import Candidate, SeriesScan
from text_utils import fuzzy_match_score, normalize_for_match
from scoring import BONUS_STRUCTURE, SCORE_THRESHOLD


ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME) {
      id
      idMal
      title { romaji english native }
      episodes
      description
      genres
      studios(isMain: true) { nodes { name } }
      coverImage { large extraLarge }
      bannerImage
      seasonYear
      averageScore
      synonyms
      status
      startDate { year month day }
    }
  }
}
"""


def _anilist_graphql_search(keyword: str) -> list[dict[str, Any]]:
    payload = {
        "query": ANILIST_SEARCH_QUERY,
        "variables": {"search": keyword},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ANILIST_GRAPHQL_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "myjbrename/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    page = data.get("data", {}).get("Page", {})
    return page.get("media", [])


def score_anilist_candidate(scan: SeriesScan, result: dict[str, Any]) -> Candidate | None:
    anilist_id = result.get("id")
    if not anilist_id:
        return None

    title = result.get("title") or {}
    names = [
        title.get("romaji") or "",
        title.get("english") or "",
        title.get("native") or "",
    ]
    for synonym in result.get("synonyms") or []:
        if synonym and synonym not in names:
            names.append(synonym)

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
    reason_flags = ["anilist_match"]

    eps_count = result.get("episodes")
    season_fit = None
    if scan.episode_count and eps_count:
        count_fit = 1.0 if eps_count == scan.episode_count else max(
            0.0,
            1 - abs(eps_count - scan.episode_count) / max(eps_count, scan.episode_count, 1),
        )
        score += count_fit * 0.3
        season_fit = {
            "source": "anilist",
            "anilist_id": anilist_id,
            "anilist_episode_count": eps_count,
            "local_episode_count": scan.episode_count,
        }
        reason_flags.append(
            "episode_count_match" if eps_count == scan.episode_count else "episode_count_mismatch"
        )
    else:
        season_fit = {"source": "anilist", "anilist_id": anilist_id}
        reason_flags.append("no_episode_count_data")

    if scan.structure in {"episode_subdirs", "season_dirs"}:
        score += BONUS_STRUCTURE
        reason_flags.append("structure_detected")

    display_name = title.get("romaji") or title.get("english") or title.get("native") or ""
    original_name = title.get("native") or title.get("romaji") or ""

    if score < SCORE_THRESHOLD:
        return None

    overview = ""
    desc = result.get("description") or ""
    if desc:
        overview = desc[:500]
    avg_score = result.get("averageScore")
    if avg_score:
        overview += f"\nAniList score: {avg_score}"

    genres = result.get("genres") or []
    studio_names = []
    studios = result.get("studios") or {}
    for node in studios.get("nodes") or []:
        name = node.get("name")
        if name:
            studio_names.append(name)

    raw = {
        "id": anilist_id,
        "name": display_name,
        "original_name": original_name,
        "overview": overview,
        "episodes": eps_count,
        "genres": genres,
        "studios": studio_names,
        "average_score": avg_score,
        "season_year": result.get("seasonYear"),
    }

    return Candidate(
        tmdb_id=anilist_id,
        name=display_name,
        original_name=original_name,
        first_air_date="",
        original_language="ja",
        overview=overview,
        score=round(max(0.0, min(score, 0.99)), 3),
        reasons=reason_flags,
        season_fit=season_fit,
        source="anilist",
        source_id=str(anilist_id),
        raw_data=raw,
    )


def enrich_with_anilist(scan: SeriesScan) -> None:
    candidates: dict[int, Candidate] = {}
    for query in scan.query_variants:
        if not query:
            continue
        try:
            results = _anilist_graphql_search(query)
        except Exception as exc:
            scan.reason_flags.append(f"anilist_search_failed:{type(exc).__name__}")
            continue
        for result in results:
            candidate = score_anilist_candidate(scan, result)
            if candidate is None:
                continue
            current = candidates.get(candidate.tmdb_id)
            if current is None or candidate.score > current.score:
                candidates[candidate.tmdb_id] = candidate
    scan.candidates = sorted(
        [*scan.candidates, *candidates.values()],
        key=lambda item: item.score, reverse=True,
    )[:5]
    if candidates:
        scan.reason_flags.append("anilist_used")


def enrich_scans_with_anilist(scans: list[SeriesScan]) -> None:
    eligible = [scan for scan in scans if scan.structure in {"episode_subdirs", "season_dirs"} and scan.episode_count]
    if not eligible:
        return
    total = len(eligible)
    for i, scan in enumerate(eligible):
        print(f"[{i+1}/{total}] AniList search: {scan.title_hint[:40]}", flush=True)
        enrich_with_anilist(scan)

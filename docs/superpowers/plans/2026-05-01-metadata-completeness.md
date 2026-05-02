# 元数据完整性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify NFO generation via a shared metadata model + adapters; add actor/voice-actor info, AniDB episode titles, and multi-image fanart download.

**Architecture:** Three new dataclasses (`SeriesMetadata`, `EpisodeMetadata`, `PersonInfo`) form the unified model. Three adapter functions (`tmdb_adapter`, `bangumi_adapter`, `anilist_adapter`) map each source into it. A single `generate_tvshow_nfo()` and `generate_episode_nfo()` consume the model. `download_images()` handles poster + backdrops + episode stills. AniDB episode titles are fetched via a new `episodes_by_aid()` UDP method and merged into the model post-adaptation.

**Tech Stack:** Python 3 stdlib (dataclasses, urllib, xml), TMDB API v3, Bangumi API, AniList data from scan cache, AniDB UDP API

---

### Task 1: Define unified metadata dataclasses

**Files:**
- Modify: `tmdb_rename.py` (insert after imports, before `INVALID_FILENAME_CHARS`)

- [ ] **Step 1: Add dataclass definitions**

Insert after the current imports block (after line 29) and before `INVALID_FILENAME_CHARS` (line 32):

```python
from dataclasses import dataclass, field


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
```

- [ ] **Step 2: Add `from __future__ import annotations` if not already present**

Check line 2 of `tmdb_rename.py` — it already has this import. No change needed.

- [ ] **Step 3: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: add SeriesMetadata, EpisodeMetadata, PersonInfo dataclasses"
```

---

### Task 2: Rewrite generate_tvshow_nfo for unified model

**Files:**
- Modify: `tmdb_rename.py` (replace existing `generate_tvshow_nfo` at lines 263-317)

- [ ] **Step 1: Replace generate_tvshow_nfo**

Replace the function at lines 263-317 with:

```python
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
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

- [ ] **Step 3: Quick smoke test**

```bash
python3 -c "
import sys; sys.path.insert(0, '/mnt/media/里番')
from tmdb_rename import SeriesMetadata, EpisodeMetadata, PersonInfo, generate_tvshow_nfo
m = SeriesMetadata(title='Test Show', original_title='Test Original', year='2024',
                    genres=['Animation'], studios=['Studio X'],
                    actors=[PersonInfo(name='Actor A', role='Voice Actor', character='Hero', order=1)],
                    poster_url='http://example.com/poster.jpg',
                    backdrop_urls=['http://example.com/fanart1.jpg'])
print(generate_tvshow_nfo(m))
"
```

Expected: valid XML with `<actor>`, `<fanart>`, `<thumb aspect="poster">poster.jpg</thumb>`.

- [ ] **Step 4: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: rewrite generate_tvshow_nfo for unified SeriesMetadata"
```

---

### Task 3: Rewrite generate_episode_nfo + delete generate_tvshow_nfo_bangumi

**Files:**
- Modify: `tmdb_rename.py` (replace `generate_episode_nfo` at lines 392-417; delete `generate_tvshow_nfo_bangumi` at lines 320-389)

- [ ] **Step 1: Replace generate_episode_nfo**

Replace the function at lines 392-417 with:

```python
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
```

- [ ] **Step 2: Delete generate_tvshow_nfo_bangumi**

Delete lines 320-389 (the entire `generate_tvshow_nfo_bangumi` function).

- [ ] **Step 3: Verify syntax and smoke test**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

```bash
python3 -c "
import sys; sys.path.insert(0, '/mnt/media/里番')
from tmdb_rename import EpisodeMetadata, generate_episode_nfo
ep = EpisodeMetadata(season=1, episode=3, title='Ep 3', title_cn='第三集', overview='desc', still_url='http://x.com/still.jpg')
print(generate_episode_nfo(ep))
"
```

Expected: valid XML with `<title>第三集</title>`, `<thumb>S01E03-thumb.jpg</thumb>`.

- [ ] **Step 4: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: rewrite generate_episode_nfo, delete generate_tvshow_nfo_bangumi"
```

---

### Task 4: Write tmdb_adapter

**Files:**
- Modify: `tmdb_rename.py` (insert new function before `_xml_escape`)

- [ ] **Step 1: Add tmdb_adapter function**

Insert before `_xml_escape` (near line 420):

```python
def tmdb_adapter(tmdb_id: int, season_number: int, api_key: str, language: str) -> SeriesMetadata | None:
    """Fetch TMDB series details, credits, and season episodes → SeriesMetadata."""
    details = tmdb_series_details(tmdb_id, api_key, language)
    if not details:
        return None

    # Credits (credits appended via append_to_response)
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
```

- [ ] **Step 2: Update tmdb_series_details to fetch credits and images**

In `tmdb_scan_preview.py`, modify `tmdb_series_details` at line 626 to append credits and images:

```python
def tmdb_series_details(series_id: int, api_key: str, language: str) -> dict[str, Any] | None:
    """Fetch full TV series details from TMDB: genres, vote_average, poster, networks, etc."""
    params = "append_to_response=credits,images"
    url = f"https://api.themoviedb.org/3/tv/{series_id}?language={urllib.parse.quote(language)}&{params}"
    try:
        return tmdb_request(url, api_key)
    except Exception:
        return None
```

- [ ] **Step 3: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_scan_preview.py', doraise=True); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add tmdb_rename.py tmdb_scan_preview.py
git commit -m "feat: add tmdb_adapter, extend tmdb_series_details with credits+images"
```

---

### Task 5: Write bangumi_adapter

**Files:**
- Modify: `tmdb_rename.py` (insert after tmdb_adapter)

- [ ] **Step 1: Add bangumi_adapter function**

```python
def bangumi_adapter(subject: dict[str, Any] | None, top_candidate: dict[str, Any] | None = None) -> SeriesMetadata | None:
    """Map Bangumi subject data → SeriesMetadata."""
    if not subject:
        return None
    top = top_candidate or {}

    name_cn = subject.get("name_cn") or ""
    name = subject.get("name") or ""
    title = name_cn or name or top.get("name", "")
    if not title:
        return None

    original_title = subject.get("name") or ""  # Japanese name
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
        # Actors are the voice actors (cv = character voice)
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

    # Episodes from subject
    eps_list = subject.get("eps") or []
    episode_count = subject.get("eps_count") or len(eps_list)
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
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: add bangumi_adapter"
```

---

### Task 6: Write anilist_adapter

**Files:**
- Modify: `tmdb_rename.py` (insert after bangumi_adapter)

- [ ] **Step 1: Add anilist_adapter function**

```python
def anilist_adapter(raw_data: dict[str, Any] | None) -> SeriesMetadata | None:
    """Map AniList raw_data (from scan candidate) → SeriesMetadata."""
    if not raw_data or not isinstance(raw_data, dict):
        return None

    title = raw_data.get("title", {}).get("romaji") or raw_data.get("title", {}).get("english") or ""
    if not title:
        return None
    original_title = raw_data.get("title", {}).get("native") or ""
    sort_title = title

    overview = (raw_data.get("description") or "").replace("<br>", "\n").replace("<br/>", "\n")
    # Strip HTML tags
    overview = re.sub(r"<[^>]+>", "", overview)

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

    # AniList doesn't provide per-episode data in search results
    episodes: list[EpisodeMetadata] = []

    return SeriesMetadata(
        title=title,
        original_title=original_title,
        sort_title=sort_title,
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
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: add anilist_adapter"
```

---

### Task 7: Add AniDB episode title enrichment

**Files:**
- Modify: `tmdb_scan_preview.py` (add `episodes_by_aid` method to `AniDBUdpClient`)
- Modify: `tmdb_rename.py` (add `anidb_enrich_episodes` function)

- [ ] **Step 1: Add episodes_by_aid to AniDBUdpClient**

Insert after the `anime_by_name` method (after line 813 in `tmdb_scan_preview.py`):

```python
    def episodes_by_aid(self, aid: int) -> list[dict[str, Any]]:
        """Fetch episode list for an AniDB anime ID. Returns list of episode dicts."""
        code, message, lines = self.command(f"EPISODE aid={aid}")
        if code in (230,):
            return []
        if code != 200 or not lines:
            raise RuntimeError(f"AniDB EPISODE lookup failed for aid={aid}: {code} {message}")
        episodes: list[dict[str, Any]] = []
        for line in lines:
            fields = [unescape_anidb_field(item) for item in line.split("|")]
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
```

- [ ] **Step 2: Add anidb_enrich_episodes to tmdb_rename.py**

Insert after `anilist_adapter`:

```python
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
```

- [ ] **Step 3: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_scan_preview.py', doraise=True); print('OK')"
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add tmdb_scan_preview.py tmdb_rename.py
git commit -m "feat: add AniDB episode title enrichment"
```

---

### Task 8: Write unified download_images

**Files:**
- Modify: `tmdb_rename.py` (insert after `anidb_enrich_episodes`)

- [ ] **Step 1: Add download_images function**

```python
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
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: add unified download_images function"
```

---

### Task 9: Rewrite download_and_write_nfo

**Files:**
- Modify: `tmdb_rename.py` (replace `download_and_write_nfo` at lines 430-637)

- [ ] **Step 1: Replace download_and_write_nfo**

Replace the function at lines 430-637 with:

```python
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
            seen: set[int] = set()
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
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add tmdb_rename.py
git commit -m "feat: rewrite download_and_write_nfo with unified adapter flow"
```

---

### Task 10: Update WebUI endpoints

**Files:**
- Modify: `app_webui.py` (update `api_download_nfo` at lines 580-624, `api_download_poster` at lines 627-684)

- [ ] **Step 1: Update api_download_nfo**

Replace the function at lines 580-624 with:

```python
def api_download_nfo():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")

    p = Path(path_str)
    if not p.is_dir():
        return {"error": "无效路径"}, 400

    api_key = _api_key()
    if not api_key:
        return {"error": "TMDB API key 未配置"}, 400

    try:
        scan = scan_series(p)
    except Exception as exc:
        return {"error": str(exc)}, 500

    if not scan.candidates:
        try:
            enrich_with_tmdb(scan, api_key, "ja-JP")
        except Exception:
            pass
        try:
            enrich_scans_with_bangumi([scan])
        except Exception:
            pass
        try:
            enrich_scans_with_anilist([scan])
        except Exception:
            pass
        try:
            enrich_scans_with_anidb_title_dump([scan], ANIDB_CACHE)
        except Exception:
            pass

    ev = evaluate_scan(scan, min_score=0.65,
                       series_title_source="tmdb", allow_fallback=True)

    try:
        nfo_count = download_and_write_nfo([ev], [scan], api_key,
                                            "ja-JP", apply=True)
        return {"success": True, "nfo_written": nfo_count,
                "series_title": ev.get("series_title", ""),
                "note": "NFO + images downloaded"}
    except Exception as exc:
        return {"error": f"NFO 下载失败: {exc}"}, 500
```

(The only change is adding `"note": "NFO + images downloaded"` to the response — `download_and_write_nfo` now handles images internally.)

- [ ] **Step 2: Update api_download_poster**

The current function works standalone. Update to report images downloaded via the new flow:

Replace the existing `api_download_poster` (lines 627-684) with a version that uses the cache's top_candidate and delegates to `download_images`:

```python
@app.route("/api/download-poster", methods=["POST"])
def api_download_poster():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")

    p = Path(path_str)
    if not p.is_dir():
        return {"error": "无效路径"}, 400

    cached = _scan_cache.get(path_str, {})
    top = cached.get("top_candidate")
    if not top:
        return {"error": "该文件夹没有匹配的元数据，请先扫描"}, 400

    # Build a minimal SeriesMetadata from the cached candidate
    from tmdb_rename import SeriesMetadata, download_images, bangumi_adapter, tmdb_adapter

    source = top.get("source", "")
    meta: SeriesMetadata | None = None

    if source == "tmdb" and top.get("tmdb_id"):
        api_key = _api_key()
        if api_key:
            try:
                meta = tmdb_adapter(
                    int(top["tmdb_id"]),
                    int((top.get("season_fit") or {}).get("season_number", 1)),
                    api_key,
                    "ja-JP",
                )
            except Exception:
                pass

    if meta is None and source == "bangumi":
        raw = top.get("raw_data")
        if raw and isinstance(raw, dict) and raw.get("id"):
            meta = bangumi_adapter(raw, top)
        elif top.get("source_id"):
            try:
                detail = bangumi_subject_details(int(top["source_id"]))
                meta = bangumi_adapter(detail, top)
            except Exception:
                pass

    if meta is None and source == "anilist":
        raw = top.get("raw_data")
        from tmdb_rename import anilist_adapter
        meta = anilist_adapter(raw)

    if meta is None:
        # Minimal: just poster URL from cached candidate overview
        poster_url = ""
        if top.get("tmdb_id") and top.get("overview", "").startswith("Bangumi"):
            pass  # Already tried bangumi above
        meta = SeriesMetadata(title=top.get("name", ""), poster_url="")

    if not meta or not meta.poster_url:
        return {"error": "未找到海报图片 URL"}, 404

    # Check if poster already exists
    poster_path = p / "poster.jpg"
    if poster_path.exists():
        return {"success": True, "path": str(poster_path), "note": "已存在"}

    saved = download_images(meta, p)
    return {"success": True, "saved": [str(s) for s in saved],
            "count": len(saved)}
```

- [ ] **Step 3: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/app_webui.py', doraise=True); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add app_webui.py
git commit -m "feat: update WebUI endpoints for unified metadata download"
```

---

### Task 11: Integration smoke test

**Files:**
- No code changes — test only

- [ ] **Step 1: Import all new components**

```bash
python3 -c "
import sys; sys.path.insert(0, '/mnt/media/里番')
from tmdb_rename import (
    SeriesMetadata, EpisodeMetadata, PersonInfo,
    generate_tvshow_nfo, generate_episode_nfo,
    tmdb_adapter, bangumi_adapter, anilist_adapter,
    anidb_enrich_episodes, download_images,
    download_and_write_nfo,
)
print('All imports OK')
"
```

- [ ] **Step 2: Round-trip test: build SeriesMetadata → NFO → parse back**

```bash
python3 -c "
import sys, xml.etree.ElementTree as ET
sys.path.insert(0, '/mnt/media/里番')
from tmdb_rename import *

meta = SeriesMetadata(
    title='Test Show',
    original_title='Test Original',
    sort_title='Test Show',
    overview='A test series.',
    first_air_date='2024-01-15',
    year='2024',
    status='Ended',
    rating='8.5',
    genres=['Animation', 'Comedy'],
    studios=['Studio X'],
    tags=['test'],
    actors=[PersonInfo(name='Actor A', role='Voice Actor', character='Hero', order=0)],
    directors=[PersonInfo(name='Director D', role='Series Director')],
    poster_url='http://example.com/poster.jpg',
    backdrop_urls=['http://example.com/fanart1.jpg', 'http://example.com/fanart2.jpg'],
    episodes=[
        EpisodeMetadata(season=1, episode=1, title='First', title_cn='第一集', still_url='http://x.com/s1.jpg'),
        EpisodeMetadata(season=1, episode=2, title='Second', title_cn='第二集'),
    ],
    source='test', source_id='123',
)

nfo = generate_tvshow_nfo(meta)
print('=== tvshow.nfo ===')
print(nfo[:500])
print('...')

# Verify it's valid XML
ET.fromstring(nfo)
print('tvshow.nfo: valid XML')

# Episode
ep_nfo = generate_episode_nfo(meta.episodes[0])
print('=== episode.nfo ===')
print(ep_nfo)
ET.fromstring(ep_nfo)
print('episode.nfo: valid XML')

print('Round-trip test PASSED')
"
```

- [ ] **Step 3: Verify no regressions — old key functions still callable**

```bash
python3 -c "
import sys; sys.path.insert(0, '/mnt/media/里番')
from tmdb_rename import _xml_escape, safe_name, choose_series_title, evaluate_scan
print('Core functions OK')
"
```

---

### Task 12: Final cleanup

**Files:**
- Modify: `tmdb_rename.py`
- Modify: `app_webui.py`

- [ ] **Step 1: Remove any unused imports in tmdb_rename.py**

The old `generate_tvshow_nfo_bangumi` is gone — make sure no references remain:

```bash
grep -n "generate_tvshow_nfo_bangumi" /mnt/media/里番/tmdb_rename.py
```

Expected: no matches.

- [ ] **Step 2: Verify final syntax for both files**

```bash
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_rename.py', doraise=True); print('tmdb_rename.py OK')"
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/app_webui.py', doraise=True); print('app_webui.py OK')"
python3 -c "import py_compile; py_compile.compile('/mnt/media/里番/tmdb_scan_preview.py', doraise=True); print('tmdb_scan_preview.py OK')"
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: final cleanup after metadata refactor"
```

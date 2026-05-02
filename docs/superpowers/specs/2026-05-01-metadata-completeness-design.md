# 元数据完整性 — 设计规格

## 目标

统一 NFO 生成流程，消除 TMDB/Bangumi 分支重复；新增 AniDB episode 标题、演员/声优信息、fanart 多图下载。

## 一、统一元数据模型

定义三个 dataclass，所有数据源最终填充到同一结构。NFO 生成器只消费这些模型，不关心数据来源。

```python
@dataclass
class PersonInfo:
    name: str
    role: str = ""       # "Voice Actor", "Director", etc.
    character: str = ""  # 配音角色名
    thumb: str = ""      # 照片 URL
    order: int = 0       # 排序

@dataclass
class EpisodeMetadata:
    season: int
    episode: int
    title: str = ""
    title_cn: str = ""   # 中文集名（AniDB/Bangumi）
    overview: str = ""
    air_date: str = ""
    rating: str = ""
    still_url: str = ""

@dataclass
class SeriesMetadata:
    title: str
    original_title: str = ""
    sort_title: str = ""
    overview: str = ""
    first_air_date: str = ""
    year: str = ""          # 从 first_air_date 派生
    status: str = ""
    rating: str = ""
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    actors: list[PersonInfo] = field(default_factory=list)
    directors: list[PersonInfo] = field(default_factory=list)
    # 图片
    poster_url: str = ""
    backdrop_urls: list[str] = field(default_factory=list)
    # 单集
    episodes: list[EpisodeMetadata] = field(default_factory=list)
    # 溯源
    source: str = ""       # "tmdb", "bangumi", "anilist"
    source_id: str = ""
```

## 二、适配器模式

每个数据源一个适配器函数，输入不同，输出统一的 `SeriesMetadata`。

### 2.1 TMDB 适配器

```
tmdb_adapter(tmdb_id: int, season: int, api_key: str, lang: str) -> SeriesMetadata
```

- 已有 `tmdb_series_details()`，加 `append_to_response=credits` 获取演员
- 已有 `tmdb_season_details()` 获取集名
- 演员从 `credits.cast` + `credits.crew` 提取（TMDB 的声优数据有限，但对里番够用）
- 图片 URL 拼前缀：`https://image.tmdb.org/t/p/original`

### 2.2 Bangumi 适配器

```
bangumi_adapter(subject_raw_data: dict) -> SeriesMetadata
```

- 已有 `bangumi_subject_details()` 提供完整数据
- 再从 subjects API 取 `crt`（角色/声优关系，responseGroup=large 已有部分字段）
- 标签映射为 genres；平台映射为 studio
- 图片用 `images.large` 和 `images.common`

### 2.3 AniList 适配器

```
anilist_adapter(raw_data: dict) -> SeriesMetadata
```

- Scan candidate 已携带 raw_data（含 title/romaji/english/format/genres/studios）
- 字段直接映射，没有演员信息则留空
- AniList 不提供集名，episodes 列表由扫描目录结构生成（空标题）

### 2.4 AniDB episode 标题增强

```
anidb_enrich_episodes(aid: int, meta: SeriesMetadata, client) -> SeriesMetadata
```

- 通过 AniDB UDP API 查 EPISODE 列表
- 将英文/罗马音集名填入 `meta.episodes[*].title`
- 在 NFO 下载流程末尾调用，非强制——失败时集名留空

## 三、图片下载统一

```
download_images(meta: SeriesMetadata, target_dir: Path) -> list[Path]
```

策略：
| 类型 | 来源字段 | 文件名 |
|------|---------|--------|
| 海报 | `poster_url` | `poster.jpg` |
| 背景 | `backdrop_urls[0]` | `fanart.jpg` |
| 额外背景 | `backdrop_urls[1:]` | `fanart{idx+1}.jpg` |
| 剧照 | `episodes[*].still_url` | `S{season}E{episode}-thumb.jpg` |

- 已存在的图片跳过不重复下载
- NFO 中 `<thumb>` 和 `<fanart>` 标签写本地文件名

## 四、NFO 生成统一

两个旧函数合并为一个：

```python
def generate_tvshow_nfo(meta: SeriesMetadata) -> str
def generate_episode_nfo(ep: EpisodeMetadata) -> str
```

删除 `generate_tvshow_nfo_bangumi`。

NFO 标签覆盖：
- `<title>`, `<originaltitle>`, `<sorttitle>`, `<plot>`, `<rating>`, `<year>`, `<premiered>`, `<status>`
- `<genre>` — 遍历 meta.genres
- `<studio>` — 第一个 studio
- `<actor>` — 遍历 meta.actors，`<name>` + `<role>` + `<character>` + `<order>` + `<thumb>`
- `<director>` — 新字段，遍历 meta.directors
- `<thumb aspect="poster">` — poster.jpg
- `<fanart>` — 包含多个 `<thumb>` 子元素，指向本地 fanart*.jpg

## 五、`download_and_write_nfo` 重写

流程：
```
1. 从 evaluated item 取 top_candidate，确定 source
2. 调用对应适配器得到 SeriesMetadata
3. 如果 source 是 AniDB 或 bangumi 且 aid 已知，调用 anidb_enrich_episodes()
4. 用 generate_tvshow_nfo(meta) 写 tvshow.nfo
5. 遍历 meta.episodes，对每个媒体文件写 episode NFO
6. 调用 download_images(meta, target_dir)
```

原有约 200 行的分支逻辑缩减为 ~60 行的线性流程。

## 六、WebUI 调整

- `api_download_nfo`：调用新 `download_and_write_nfo`，自动包含图片下载
- `api_download_poster`：保持兼容，内部改为调用 `download_images`

## 七、不改动的部分

- `tmdb_scan_preview.py` — 搜索/评分/候选生成逻辑不碰
- AniDB 标题dump 匹配逻辑不碰
- 重命名逻辑（`plan_episode` 等）不碰

## 八、文件变更范围

| 文件 | 变更 |
|------|------|
| `tmdb_rename.py` | 新增 dataclass + 适配器 + 重写 NFO 生成 + 图片下载 |
| `app_webui.py` | `api_download_nfo` / `api_download_poster` 适配新接口 |

预计新增 ~250 行，删除 ~150 行。

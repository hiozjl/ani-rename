# 动漫重命名 WebUI

一个面向本地媒体库的动漫/OVA 批量重命名工具。它会扫描每个作品目录，尝试从 TMDB、Bangumi、AniList、AniDB 等来源匹配元数据，并生成类似：

```text
作品名 - S01E01 - 集名.mkv
```

同时支持下载 `tvshow.nfo`、单集 NFO、`poster.jpg`、`fanart.jpg` 等媒体库文件。

## 快速启动

### Docker Compose（推荐）

1. 准备 TMDB API key：
   - 写入项目根目录的 `tmdb-api.txt`；或
   - 使用环境变量 `TMDB_API_KEY`。
2. 先以只读模式启动，用于扫描和预览：

```bash
docker compose up -d --build
```

默认会把 `/mediapath` 挂载到容器 `/media`，端口为 `5800`。

如需修改媒体目录：

```bash
MEDIA_ROOT=/你的/媒体目录 docker compose up -d --build
```

3. 确认预览结果无误后，如果要在 WebUI 执行改名，使用可写挂载重新启动：

```bash
MEDIA_VOLUME_MODE=rw docker compose up -d
```

访问：`http://你的主机IP:5800`

> 安全建议：默认只读是为了避免首次使用时误改名。只有确认预览结果后再开启 `MEDIA_VOLUME_MODE=rw`。

### 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app_webui.py --root /你的/媒体目录 --host 127.0.0.1 --port 5800
```

如果需要局域网访问，把 `--host` 改为 `0.0.0.0`。

## WebUI 使用流程

1. 打开 WebUI，点击 **扫描全部** 或 **增量扫描**。
2. 查看每张卡片的匹配结果、来源和置信度。
3. 对不满意的项目，手动编辑“系列名称”，点击 **预览**。
4. 只勾选确认无误的项目，再点击 **应用全部改名**。
5. 按需下载 **NFO** 和 **海报**。

## 安全机制

- WebUI 接口会限制传入路径必须位于启动参数 `--root` 指定的媒体根目录下。
- Docker 默认以只读挂载启动，扫描/预览安全；改名需要显式设置 `MEDIA_VOLUME_MODE=rw`。
- 批量改名只会应用当前已勾选的项目，不再默认处理整个媒体库。
- 文件夹改名和文件改名是分开的按钮，避免批量操作中误改目录名。

## API Key 配置

TMDB key 支持两种方式：

1. 环境变量：

```bash
TMDB_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx python app_webui.py --root /media
```

2. 项目根目录文件：

```bash
echo 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' > tmdb-api.txt
```

环境变量优先级高于 `tmdb-api.txt`。

## 常见问题

### 扫描正常，但改名失败

如果使用 Docker，请确认媒体卷是可写的：

```bash
MEDIA_VOLUME_MODE=rw docker compose up -d
```

### 页面样式加载不出来

当前页面使用 jsDelivr 加载 Bootstrap。离线或内网环境可能无法访问 CDN，后续可改为本地静态资源。

### 匹配结果不准

可以在卡片里的“系列名称”手动输入名称，然后点击 **预览** 查看将要发生的改名操作。确认后再勾选并应用。

## 命令行工具

项目也包含 CLI：

- `tmdb_scan_preview.py`：扫描并生成预览 manifest。
- `tmdb_apply_manifest.py`：从 manifest 中选择已批准项目执行改名。
- `tmdb_rename.py`：直接扫描、评估、预览或执行改名。

建议新用户优先使用 WebUI。

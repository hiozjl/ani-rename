#!/usr/bin/env python3
"""里番重命名 WebUI — 自动/手动改名 + 元数据匹配"""

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import flask

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tmdb_scan_preview import (
    bangumi_subject_details,
    quick_fingerprint,
    fingerprint_changed,
    scan_series,
    series_paths,
    tmdb_series_details,
)
from enrich import enrich_all_sources
from tmdb_rename import (
    apply_operations,
    download_and_write_nfo,
    evaluate_scan,
    plan_episode,
    summarize,
)

APP_DIR = Path(__file__).parent.resolve()
MEDIA_ROOT = APP_DIR  # 默认等于代码目录，可通过 --root 覆盖
TMDB_KEY_FILE = APP_DIR / "tmdb-api.txt"
ANIDB_CACHE = APP_DIR / "anidb-title-cache" / "anime-titles.xml.gz"

app = flask.Flask(__name__)


# ── helpers ────────────────────────────────────────────────────────────────

def _read_tmdb_key() -> str:
    env_key = os.environ.get("TMDB_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return TMDB_KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _api_key():
    key = _read_tmdb_key()
    return key if re.fullmatch(r"[0-9a-fA-F]{32}", key or "") else ""


def _media_path(path_str: str) -> Path | None:
    """Resolve and constrain user-provided paths to the configured media root."""
    if not path_str:
        return None
    try:
        root = MEDIA_ROOT.resolve()
        path = Path(path_str).resolve()
        if path == root or path.is_relative_to(root):
            return path
    except (OSError, RuntimeError, ValueError):
        return None
    return None


# ── background scanner ─────────────────────────────────────────────────────

@dataclass
class ScanProgress:
    running: bool = False
    cancel: bool = False
    total: int = 0
    current: int = 0
    current_name: str = ""
    results: list[dict] = field(default_factory=list)


_scan_progress = ScanProgress()
_scan_cache: dict[str, dict] = {}  # path -> serialized scan result
_CACHE_FILE = APP_DIR / "webui_scan_cache.json"


def _save_cache():
    try:
        serializable = {k: {kk: vv for kk, vv in v.items()
                            if kk not in ("top_candidate", "operations") or vv is not None}
                        for k, v in _scan_cache.items()}
        _CACHE_FILE.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_cache():
    if _CACHE_FILE.exists():
        try:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            _scan_cache.clear()
            _scan_cache.update(data)
        except Exception:
            pass


_load_cache()


def _get_anime_folders() -> list[Path]:
    skip = {"__pycache__", "anidb-title-cache", ".codex", ".omx",
            ".tmdb-apply-test", ".tmdb-cli-test", ".claude", ".superpowers"}
    try:
        return sorted(
            [p for p in MEDIA_ROOT.iterdir() if p.is_dir() and p.name not in skip],
            key=lambda p: p.name,
        )
    except OSError:
        return []


def _scan_one_impl(p: Path, enrich: bool = True, override_title: str = "") -> dict:
    """Core scan logic (no caching). If override_title is provided, use it
    for searches and as the result series_title."""
    try:
        scan = scan_series(p)
    except Exception as exc:
        return {"path": str(p), "status": "error", "reason": f"扫描失败: {exc}"}

    if override_title:
        scan.query_variants = [override_title] + scan.query_variants
        scan.title_hint = override_title

    # 尝试用缓存的 ID 直接匹配（不管文件夹改了什么名字都能对上）
    prev = _scan_cache.get(str(p), {})
    prev_cand = prev.get("top_candidate")
    if prev_cand and prev_cand.get("source_id"):
        from tmdb_scan_preview import Candidate
        src = prev_cand.get("source", "")
        sid = prev_cand.get("source_id", "")
        tmdb_id = prev_cand.get("tmdb_id", 0)
        name = prev_cand.get("name", "")
        zh = prev_cand.get("zh_title", "")
        # try direct ID lookup
        direct_name = name
        api_key = _api_key()
        if src == "tmdb" and tmdb_id:
            try:
                details = tmdb_series_details(int(tmdb_id), api_key, "ja-JP")
                if details:
                    direct_name = details.get("name") or name
            except Exception:
                pass
        elif src == "bangumi":
            try:
                detail = bangumi_subject_details(int(sid))
                if detail:
                    direct_name = detail.get("name_cn") or detail.get("name") or name
            except Exception:
                pass
        # inject a high-confidence candidate
        known = Candidate(
            tmdb_id=tmdb_id if src == "tmdb" else 0,
            name=direct_name,
            original_name=prev_cand.get("original_name", ""),
            first_air_date=prev_cand.get("first_air_date", ""),
            original_language=prev_cand.get("original_language", ""),
            overview=prev_cand.get("overview", ""),
            score=0.99,
            reasons=["reused_previous_match"],
            source=src,
            source_id=sid,
            zh_title=zh,
            raw_data=prev_cand.get("raw_data"),
        )
        scan.candidates = [known]

    # 如果没找到缓存 ID，尝试从 tvshow.nfo / poster.jpg 判断是否有过匹配
    if not scan.candidates:
        nfo = p / "tvshow.nfo"
        if nfo.exists():
            try:
                import xml.etree.ElementTree as ET
                tree = ET.parse(str(nfo))
                root = tree.getroot()
                for field in ("originaltitle", "title"):
                    val = (root.findtext(field) or "").strip()
                    if val and val != scan.title_hint and val not in scan.query_variants:
                        scan.query_variants.insert(0, val)
            except Exception:
                pass

    if enrich:
        try:
            enrich_all_sources(scan, tmdb_api_key=_api_key(), anidb_cache=ANIDB_CACHE)
        except Exception:
            pass

    ev = evaluate_scan(scan, min_score=0.65,
                       series_title_source="tmdb", allow_fallback=True)

    # override series_title with user's manual name if provided and no better match
    if override_title and (not ev.get("top_candidate") or (ev["top_candidate"].get("score", 0) or 0) < 0.7):
        ev["series_title"] = override_title

    return {
        "path": str(scan.path),
        "name": scan.title_hint,
        "structure": scan.structure,
        "episode_count": scan.episode_count,
        "status": ev["status"],
        "reason": ev["reason"],
        "series_title": ev.get("series_title", scan.title_hint),
        "top_candidate": ev.get("top_candidate"),
        "operations": ev.get("operations", []),
    }


def _scan_background(mode: str = "full"):
    """mode: 'full' → scan everything; 'incremental' → only scan unmatched/changed items"""
    global _scan_progress
    _scan_progress = ScanProgress(running=True)
    folders = _get_anime_folders()
    _scan_progress.results = []

    # In incremental mode, pre-filter: skip items with good cached matches
    scan_folders: list[Path] = []
    skipped_count = 0
    for p in folders:
        path_str = str(p)
        cached = _scan_cache.get(path_str, {})
        if mode == "incremental" and cached:
            # Skip items already matched with a candidate and unchanged
            if cached.get("top_candidate") and cached.get("status") in ("noop", "rename"):
                # Quick fingerprint check
                try:
                    fp = quick_fingerprint(p)
                    cached_fp = cached.get("fingerprint", {})
                    if not fingerprint_changed({"fingerprint": cached_fp}, fp):
                        _scan_progress.results.append(cached)
                        skipped_count += 1
                        continue
                except Exception:
                    pass  # on fingerprint error, just re-scan
        scan_folders.append(p)

    _scan_progress.total = len(folders)  # total includes skipped items for accurate progress
    if skipped_count:
        print(f"Incremental scan: skipping {skipped_count} matched items, scanning {len(scan_folders)}")

    for i, p in enumerate(scan_folders):
        if _scan_progress.cancel:
            break
        _scan_progress.current = skipped_count + i + 1  # 1-indexed, includes already-skipped
        _scan_progress.current_name = p.name
        try:
            r = _scan_one_impl(p, enrich=True)
            # Preserve fingerprint for future incremental checks
            try:
                r["fingerprint"] = quick_fingerprint(p)
            except Exception:
                pass
            _scan_cache[str(p)] = r
            _scan_progress.results.append(r)
            _save_cache()
        except Exception as exc:
            r = {"path": str(p), "name": p.name, "status": "error",
                 "reason": str(exc)[:200]}
            _scan_progress.results.append(r)

    _scan_progress.running = False


# ── API routes ─────────────────────────────────────────────────────────────

@app.route("/api/folders")
def api_folders():
    folders = _get_anime_folders()
    items = []
    for p in folders:
        cached = _scan_cache.get(str(p), {})
        # check existence of assets
        nfo_exists = (p / "tvshow.nfo").exists()
        poster_exists = (p / "poster.jpg").exists()
        items.append({
            "name": p.name,
            "path": str(p),
            "structure": cached.get("structure", ""),
            "episode_count": cached.get("episode_count", 0),
            "status": cached.get("status", ""),
            "series_title": cached.get("series_title", ""),
            "reason": cached.get("reason", ""),
            "scanned": bool(cached),
            "top_candidate": cached.get("top_candidate"),
            "operations": cached.get("operations", []),
            "nfo_exists": nfo_exists,
            "poster_exists": poster_exists,
        })
    return {"items": items, "total": len(items)}


@app.route("/api/scan-start", methods=["POST"])
def api_scan_start():
    if _scan_progress.running:
        return {"error": "扫描正在进行中"}, 409
    data = flask.request.get_json(silent=True) or {}
    mode = data.get("mode", "full")
    t = threading.Thread(target=_scan_background, kwargs={"mode": mode}, daemon=True)
    t.start()
    return {"status": "started", "mode": mode}


@app.route("/api/scan-incremental", methods=["POST"])
def api_scan_incremental():
    """Scan only unmatched/skip/error items, skip already-matched ones."""
    if _scan_progress.running:
        return {"error": "扫描正在进行中"}, 409
    t = threading.Thread(target=_scan_background, kwargs={"mode": "incremental"}, daemon=True)
    t.start()
    return {"status": "started", "mode": "incremental"}


@app.route("/api/scan-status")
def api_scan_status():
    s = _scan_progress
    return {
        "running": s.running,
        "total": s.total,
        "current": s.current,
        "current_name": s.current_name,
        "cancel": s.cancel,
    }


@app.route("/api/scan-cancel", methods=["POST"])
def api_scan_cancel():
    _scan_progress.cancel = True
    return {"status": "cancelling"}


@app.route("/api/scan-results")
def api_scan_results():
    s = _scan_progress
    summary = summarize(s.results) if s.results else {}
    return {"results": s.results, "summary": summary, "total": len(s.results),
            "done": not s.running}


@app.route("/api/scan-one", methods=["POST"])
def api_scan_one():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")
    series_title = data.get("series_title", "")
    p = _media_path(path_str)
    if not p or not p.is_dir():
        return {"error": "无效路径"}, 400
    result = _scan_one_impl(p, enrich=True, override_title=series_title)
    _scan_cache[str(p)] = result
    _save_cache()
    return result


@app.route("/api/preview-one", methods=["POST"])
def api_preview_one():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")
    series_title = data.get("series_title", "")

    p = _media_path(path_str)
    if not p or not p.is_dir():
        return {"error": "无效路径"}, 400

    try:
        scan = scan_series(p)
    except Exception as exc:
        return {"error": str(exc)}, 500

    if not scan.candidates:
        try:
            enrich_all_sources(scan, tmdb_api_key=_api_key(), anidb_cache=ANIDB_CACHE)
        except Exception:
            pass

    ev = evaluate_scan(scan, min_score=0.65,
                       series_title_source="tmdb", allow_fallback=True)

    if series_title:
        ev["series_title"] = series_title
        ops = []
        for episode in scan.episodes:
            ops.extend(plan_episode(scan, episode, series_title))
        current = Path(scan.path).name
        if series_title and series_title != current:
            ops.append({
                "status": "rename_folder",
                "path": str(scan.path),
                "target": str(Path(scan.path).parent / series_title),
                "reason": "manual_override",
            })
        ev["operations"] = ops

    return ev


@app.route("/api/apply-one", methods=["POST"])
def api_apply_one():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")
    series_title = data.get("series_title", "")

    p = _media_path(path_str)
    if not p or not p.is_dir():
        return {"error": "无效路径"}, 400

    try:
        scan = scan_series(p)
    except Exception as exc:
        return {"error": str(exc)}, 500

    if not scan.candidates:
        try:
            enrich_all_sources(scan, tmdb_api_key=_api_key(), anidb_cache=ANIDB_CACHE)
        except Exception:
            pass

    ev = evaluate_scan(scan, min_score=0.65,
                       series_title_source="tmdb", allow_fallback=True)

    if series_title:
        ev["series_title"] = series_title
        ops = []
        for episode in scan.episodes:
            ops.extend(plan_episode(scan, episode, series_title))
        ev["operations"] = ops

    # 过滤掉文件夹重命名——只改名文件，保持文件夹原名以利后续扫描
    ev["operations"] = [op for op in ev["operations"] if op["status"] != "rename_folder"]

    renamed, errors = apply_operations([ev], apply=True)
    ev["applied_renamed"] = renamed
    ev["applied_errors"] = errors

    _scan_cache.pop(path_str, None)
    _save_cache()
    return ev


@app.route("/api/rename-folder", methods=["POST"])
def api_rename_folder():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")
    new_name = data.get("new_name", "").strip()

    if not path_str or not new_name:
        return {"error": "参数缺失"}, 400

    from tmdb_rename import safe_name
    new_name = safe_name(new_name)
    src = _media_path(path_str)

    if not src or not src.is_dir():
        return {"error": "源目录不存在"}, 404
    dst = src.parent / new_name
    if not (dst.resolve().parent == MEDIA_ROOT.resolve()):
        return {"error": "目标目录不在媒体根目录下"}, 400
    if dst.exists():
        return {"error": f"目标目录已存在: {new_name}"}, 409

    try:
        src.rename(dst)
        _scan_cache.pop(str(src), None)
        _scan_cache.pop(str(dst), None)
        _save_cache()
        return {"success": True, "old_path": str(src), "new_path": str(dst),
                "new_name": new_name}
    except OSError as exc:
        return {"error": f"重命名失败: {exc}"}, 500


@app.route("/api/apply-all", methods=["POST"])
def api_apply_all():
    data = flask.request.get_json(silent=True) or {}
    overrides = data.get("overrides", {})
    selected_paths = data.get("paths", [])

    if selected_paths:
        folders = []
        for item in selected_paths:
            p = _media_path(str(item))
            if not p or not p.is_dir():
                return {"error": f"无效路径: {item}"}, 400
            folders.append(p)
    else:
        return {"error": "请选择要应用的项目"}, 400
    results = []
    total_renamed = 0
    total_errors = 0

    for p in folders:
        path_str = str(p)
        series_title = overrides.get(path_str, "")

        try:
            scan = scan_series(p)
        except Exception:
            results.append({"path": path_str, "status": "error",
                           "reason": "scan_failed"})
            total_errors += 1
            continue

        if not scan.candidates:
            try:
                enrich_all_sources(scan, tmdb_api_key=_api_key(), anidb_cache=ANIDB_CACHE)
            except Exception:
                pass

        ev = evaluate_scan(scan, min_score=0.65,
                           series_title_source="tmdb", allow_fallback=True)

        if series_title:
            ev["series_title"] = series_title
            ops = []
            for episode in scan.episodes:
                ops.extend(plan_episode(scan, episode, series_title))
            ev["operations"] = ops

        # 过滤掉文件夹重命名——只改名文件，保持文件夹原名以利后续扫描
        ev["operations"] = [op for op in ev["operations"] if op["status"] != "rename_folder"]

        renamed, errors = apply_operations([ev], apply=True)
        total_renamed += renamed
        total_errors += errors
        ev["applied_renamed"] = renamed
        ev["applied_errors"] = errors
        results.append(ev)
        _scan_cache.pop(path_str, None)

    _save_cache()
    return {"total": len(results), "total_renamed": total_renamed,
            "total_errors": total_errors, "results": results}


@app.route("/api/download-nfo", methods=["POST"])
def api_download_nfo():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")

    p = _media_path(path_str)
    if not p or not p.is_dir():
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
            enrich_all_sources(scan, tmdb_api_key=api_key, anidb_cache=ANIDB_CACHE)
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


@app.route("/api/download-poster", methods=["POST"])
def api_download_poster():
    data = flask.request.get_json(silent=True) or {}
    path_str = data.get("path", "")

    p = _media_path(path_str)
    if not p or not p.is_dir():
        return {"error": "无效路径"}, 400

    cached = _scan_cache.get(path_str, {})
    top = cached.get("top_candidate")
    if not top:
        return {"error": "该文件夹没有匹配的元数据，请先扫描"}, 400

    from tmdb_rename import SeriesMetadata, download_images, bangumi_adapter, tmdb_adapter, anilist_adapter

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
        meta = anilist_adapter(raw)

    if meta is None:
        # Minimal metadata with whatever poster info we can find
        meta = SeriesMetadata(title=top.get("name", ""))

    # Check if poster already exists
    poster_path = p / "poster.jpg"
    if poster_path.exists():
        return {"success": True, "path": str(poster_path), "note": "已存在"}

    saved = download_images(meta, p)
    return {"success": True, "saved": [str(s) for s in saved],
            "count": len(saved)}


@app.route("/")
def index():
    folders = _get_anime_folders()
    return flask.render_template_string(HTML_TEMPLATE,
                                        folders=[p.name for p in folders],
                                        root_path=str(MEDIA_ROOT))


# ── error handler ──────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def _handle_exception(exc):
    if app.debug:
        raise
    tb = traceback.format_exc()
    print(f"[WebUI Error] {exc}\n{tb}", flush=True)
    return {"error": str(exc)[:200]}, 500


# ── HTML template ──────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>里番重命名 WebUI</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{--bg:#1a1a2e;--card:#16213e;--card-hover:#1a2744;--border:#2a2a4a;--text:#e0e0e0;--muted:#888;--accent:#0f7bff;--accent-hover:#0a6ae0;--success:#28a745;--warning:#ffc107;--danger:#dc3545;--info:#17a2b8;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,-apple-system,sans-serif;min-height:100vh}
.navbar{background:linear-gradient(135deg,#0f3460,#16213e)!important;border-bottom:1px solid var(--border);box-shadow:0 2px 12px rgba(0,0,0,.3)}
.navbar-brand{font-weight:700;font-size:1.3rem;letter-spacing:.5px}
.toolbar{background:var(--card);padding:12px 20px;border-radius:12px;margin-bottom:16px;border:1px solid var(--border);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.toolbar .btn{white-space:nowrap}
.folder-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;transition:all .2s;height:100%}
.folder-card:hover{background:var(--card-hover);border-color:var(--accent)}
.folder-card .card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;gap:8px}
.folder-card .folder-name{font-size:1rem;font-weight:600;word-break:break-all;flex:1}
.folder-card .folder-name-input{font-size:.9rem;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 8px;margin-top:2px}
.folder-card .folder-name-input:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px rgba(15,123,255,.2)}
.badge-status{font-size:.75rem;padding:3px 8px;border-radius:20px;white-space:nowrap;flex-shrink:0;font-weight:600}
.folder-meta{font-size:.82rem;color:var(--muted);margin-bottom:8px;display:flex;flex-wrap:wrap;gap:8px}
.folder-meta span{background:rgba(255,255,255,.05);padding:2px 8px;border-radius:4px}
.candidate-info{background:rgba(15,123,255,.1);border-left:3px solid var(--accent);padding:6px 10px;border-radius:4px;font-size:.82rem;margin-bottom:8px}
.op-list{font-size:.8rem;margin-top:6px;max-height:160px;overflow-y:auto}
.op-list .op-item{padding:2px 6px;border-radius:3px;margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.op-rename{color:var(--success)}
.op-noop{color:var(--muted)}
.op-error{color:var(--danger)}
.op-rename-folder{color:var(--info)}
.card-actions{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px;border-top:1px solid var(--border);padding-top:8px}
.card-actions .btn{font-size:.78rem;padding:2px 8px}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid var(--text);border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:4px}
@keyframes spin{to{transform:rotate(360deg)}}
.toast-container{z-index:1060}
#toast{opacity:0;transition:opacity .3s;pointer-events:none}
#toast.show{opacity:1;pointer-events:auto}
#progress-bar-wrap{display:none;background:var(--card);padding:10px 16px;border-radius:8px;margin-bottom:12px;border:1px solid var(--border)}
.progress{height:8px;background:var(--bg);border-radius:4px}
.progress-bar{transition:width .5s;background:var(--accent)}
#scan-progress-text{font-size:.82rem;color:var(--muted);margin-top:4px}
.search-box input{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 14px;font-size:.85rem}
.search-box input:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px rgba(15,123,255,.2)}
.grid-loading{text-align:center;padding:40px;color:var(--muted)}
.filter-btns .btn{font-size:.8rem;padding:2px 10px}
.empty-state{text-align:center;padding:60px 20px;color:var(--muted)}
.empty-state .icon{font-size:3rem;margin-bottom:12px}
</style>
</head>
<body>

<nav class="navbar navbar-dark px-3 py-2 d-flex justify-content-between">
  <span class="navbar-brand">🎬 里番重命名</span>
  <span class="text-muted small" id="count-badge">加载中...</span>
</nav>

<div class="container-fluid px-3 py-3">

  <!-- Toolbar -->
  <div class="toolbar">
    <button class="btn btn-primary btn-sm" onclick="startScan()" id="btn-scan-all">🔍 扫描全部</button>
    <button class="btn btn-outline-primary btn-sm" onclick="startIncrementalScan()" id="btn-scan-incr">⚡ 增量扫描</button>
    <button class="btn btn-warning btn-sm" onclick="cancelScan()" id="btn-cancel-scan" style="display:none">⏹ 停止</button>
    <button class="btn btn-success btn-sm" onclick="applyAll()">✅ 应用全部改名</button>
    <button class="btn btn-info btn-sm" onclick="downloadAllNfo()" id="btn-download-all-nfo">📄 下载全部 NFO</button>
    <button class="btn btn-outline-light btn-sm" onclick="downloadAllPosters()" id="btn-download-all-posters">🖼️ 下载全部海报</button>
    <button class="btn btn-outline-light btn-sm" onclick="refreshFolders()">🔄 刷新</button>
    <div class="ms-auto d-flex gap-2 align-items-center">
      <div class="search-box">
        <input type="text" id="search-input" placeholder="搜索文件夹..." oninput="filterCards()" style="width:160px">
      </div>
      <div class="filter-btns d-flex gap-1">
        <button class="btn btn-sm btn-outline-secondary active" data-filter="all" onclick="setFilter(this,'all')">全部</button>
        <button class="btn btn-sm btn-outline-success" data-filter="rename" onclick="setFilter(this,'rename')">待改名/需操作</button>
        <button class="btn btn-sm btn-outline-warning" data-filter="skip" onclick="setFilter(this,'skip')">跳过/未匹配</button>
      </div>
    </div>
  </div>

  <!-- Summary -->
  <div id="summary-bar" style="display:none;margin-bottom:8px;font-size:.85rem;color:var(--muted)"></div>

  <!-- Progress -->
  <div id="progress-bar-wrap">
    <div class="progress"><div class="progress-bar" id="scan-progress-bar" style="width:0%"></div></div>
    <div id="scan-progress-text">准备扫描...</div>
  </div>

  <!-- Grid -->
  <div id="folder-grid" class="row g-3">
    <div class="col-12 text-center text-muted py-5"><div class="spinner"></div>加载中...</div>
  </div>
</div>

<!-- Toast -->
<div class="toast-container position-fixed bottom-0 end-0 p-3">
  <div id="toast" class="toast align-items-center text-bg-dark border-0" role="alert">
    <div class="d-flex"><div class="toast-body" id="toast-msg"></div>
    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>
  </div>
</div>

<script>
const ROOT_PATH = "{{ root_path|safe }}";
let folders = [];
let currentFilter = 'all';
let searchTerm = '';
let scanPollTimer = null;
let selectedPaths = new Set();

function showToast(msg, type='info') {
  const t = document.getElementById('toast');
  const msgEl = document.getElementById('toast-msg');
  t.classList.remove('text-bg-dark','text-bg-success','text-bg-danger','text-bg-warning','text-bg-info');
  t.classList.add('text-bg-' + (type==='error'?'danger':type==='success'?'success':type==='warning'?'warning':type==='info'?'info':'dark'));
  msgEl.textContent = msg;
  t.classList.add('show');
  // auto-hide after 4s
  clearTimeout(t._timeout);
  t._timeout = setTimeout(() => t.classList.remove('show'), 4000);
}

function badgeCls(s) { return {'rename':'success','noop':'secondary','skip':'warning','unmatched':'danger','error':'danger','':'secondary'}[s]||'secondary'; }

function statusLabel(item) {
  if (!item.scanned) return '未扫描';
  const s = item.status;
  if (s === 'rename') return '🔄 待改名';
  if (s === 'noop') return '✅ 无需改';
  if (s === 'skip') {
    const r = item.reason||'';
    if (r.includes('unsupported_structure')) return '⏭ 结构不支持';
    if (r.includes('score_below')) { const m=r.match(/score_below_threshold:(.+)/); return `⏭ 分数过低(${m?m[1]:'?'})`; }
    return '⏭ 跳过';
  }
  if (s === 'unmatched') return '❓ 无匹配';
  if (s === 'error') return '❌ 错误';
  return s;
}

function reasonLabel(item) {
  if (!item.scanned || !item.reason) return '';
  const r = item.reason||'';
  if (r.startsWith('top_candidate:')) return '✅ 匹配: ' + r.slice(14).substring(0,40);
  if (r.includes('unsupported_structure')) return '📐 仅支持集号子目录结构';
  if (r.includes('score_below_threshold')) { const m=r.match(/score_below_threshold:(.+)/); return `📉 匹配置信度过低 (${m?m[1]:'?'})`; }
  if (r.includes('no_metadata_candidate')) return '🔍 未找到匹配项';
  if (r.includes('fallback_no_metadata')) return '📝 无元数据，使用目录名';
  return r.substring(0,50);
}

function renderCard(item) {
  const fm = currentFilter === 'all'
    || (currentFilter === 'rename' && (item.status === 'rename' || item.status === 'error'))
    || (currentFilter === 'skip' && (item.status === 'skip' || item.status === 'unmatched'));
  if (searchTerm && !item.name.toLowerCase().includes(searchTerm.toLowerCase())) return null;
  if (!fm) return null;

  const card = document.createElement('div');
  card.className = 'col-xl-3 col-lg-4 col-md-6';
  card.dataset.path = item.path;

  const inner = document.createElement('div');
  inner.className = 'folder-card';

  const header = document.createElement('div');
  header.className = 'card-header';
  const selectBox = document.createElement('input');
  selectBox.type = 'checkbox';
  selectBox.className = 'form-check-input mt-1';
  selectBox.title = '勾选后可批量应用';
  selectBox.disabled = item.status !== 'rename';
  selectBox.checked = item.status === 'rename' && selectedPaths.has(item.path);
  selectBox.onchange = function() {
    if (selectBox.checked) selectedPaths.add(item.path);
    else selectedPaths.delete(item.path);
  };
  const nameDiv = document.createElement('div');
  nameDiv.className = 'folder-name';
  nameDiv.textContent = item.name;
  const badge = document.createElement('span');
  badge.className = `badge-status badge bg-${badgeCls(item.status)}`;
  badge.textContent = statusLabel(item);
  header.append(selectBox, nameDiv, badge);

  const meta = document.createElement('div');
  meta.className = 'folder-meta';
  if (item.structure) { const s = document.createElement('span'); s.textContent = '📁 '+item.structure; meta.appendChild(s); }
  if (item.episode_count) { const s = document.createElement('span'); s.textContent = '🎬 '+item.episode_count+'集'; meta.appendChild(s); }
  if (item.poster_exists) { const s = document.createElement('span'); s.textContent = '🖼️ 海报'; s.style.color='var(--success)'; meta.appendChild(s); }
  if (item.nfo_exists) { const s = document.createElement('span'); s.textContent = '📄 NFO'; s.style.color='var(--info)'; meta.appendChild(s); }
  if (item.scanned) { const rl = reasonLabel(item); if (rl) { const s = document.createElement('span'); s.textContent = rl; s.style.color = item.status==='rename'?'var(--success)':item.status==='skip'?'var(--warning)':'var(--muted)'; meta.appendChild(s); } }

  let candDiv = null;
  if (item.top_candidate) {
    const tc = item.top_candidate;
    candDiv = document.createElement('div');
    candDiv.className = 'candidate-info';
    const t = tc.name||'';
    const sc = tc.score ? ` (${(tc.score*100).toFixed(0)}%)` : '';
    const src = tc.source ? ` [${tc.source}]` : '';
    let txt = `🏆 ${t}${sc}${src}`;
    if (item.series_title && item.series_title !== item.name) txt += ` → ${item.series_title}`;
    candDiv.textContent = txt;
  }

  const titleRow = document.createElement('div');
  titleRow.className = 'mb-1';
  const label = document.createElement('label');
  label.className = 'form-label small text-muted mb-0';
  label.textContent = '系列名称:';
  const input = document.createElement('input');
  input.className = 'folder-name-input';
  input.type = 'text';
  input.value = item.series_title || item.name;
  titleRow.append(label, input);
  if (item.top_candidate) {
    const fillBtn = document.createElement('button');
    fillBtn.className = 'btn btn-sm p-0 ms-1';
    fillBtn.style.cssText = 'font-size:1rem;line-height:1;border:none;background:transparent;cursor:pointer';
    fillBtn.textContent = '📋';
    fillBtn.title = '填入匹配名称';
    fillBtn.onclick = function() { input.value = item.top_candidate.name; };
    titleRow.appendChild(fillBtn);
  }

  let opList = null;
  if (item.operations && item.operations.length) {
    opList = document.createElement('div');
    opList.className = 'op-list';
    for (const op of item.operations) {
      const d = document.createElement('div');
      d.className = 'op-item ' + (op.status==='rename'?'op-rename':op.status==='rename_folder'?'op-rename-folder':op.status==='noop'?'op-noop':'op-error');
      const fname = op.path ? op.path.split('/').pop() : '';
      if (op.status === 'rename_folder') d.textContent = `📁 ${fname} → ${op.target.split('/').pop()}`;
      else if (op.status === 'rename') d.textContent = `📄 ${fname} → ${op.target.split('/').pop()}`;
      else if (op.status === 'noop') d.textContent = `✅ ${fname}`;
      else d.textContent = `❌ ${fname}: ${op.reason||''}`;
      opList.appendChild(d);
    }
  }

  const actions = document.createElement('div');
  actions.className = 'card-actions';
  const makeBtn = (cls, txt, fn) => { const b=document.createElement('button'); b.className='btn '+cls; b.onclick=function(ev){fn(b)}; b.textContent=txt; actions.appendChild(b); };
  makeBtn('btn-outline-info btn-sm', '🔍 扫描', (btn)=>scanOne(item.path, input));
  makeBtn('btn-outline-success btn-sm', '👁 预览', (btn)=>previewOne(item.path, input));
  makeBtn('btn-success btn-sm', '✅ 改名', (btn)=>applyOne(item.path, input));
  makeBtn('btn-outline-warning btn-sm', '📁 文件夹', (btn)=>renameFolder(item.path, input));
  makeBtn('btn-outline-info btn-sm', '📄 NFO', (btn)=>downloadNfo(item.path, null, btn));
  makeBtn('btn-outline-light btn-sm', '🖼️ 海报', (btn)=>downloadPoster(item.path, btn));

  inner.append(header, meta);
  if (candDiv) inner.appendChild(candDiv);
  inner.appendChild(titleRow);
  if (opList) inner.appendChild(opList);
  inner.appendChild(actions);
  card.appendChild(inner);
  return card;
}

function refreshSingleCard(item) {
  const grid = document.getElementById('folder-grid');
  const oldCard = grid.querySelector(`[data-path="${CSS.escape(item.path)}"]`);
  const newCard = renderCard(item);
  if (oldCard && newCard) {
    oldCard.replaceWith(newCard);
  } else if (!oldCard && newCard) {
    // Card was filtered out before, now visible - just append
    grid.appendChild(newCard);
  } else if (oldCard && !newCard) {
    // Card is no longer visible under current filter - remove it
    oldCard.remove();
  }
}

function updateSummary() {
  let sren=0,snoop=0,sskip=0,sunm=0,serr=0,sunscanned=0;
  for (const item of folders) {
    if (item.status === 'rename') sren++;
    else if (item.status === 'noop') snoop++;
    else if (item.status === 'skip') sskip++;
    else if (item.status === 'unmatched') sunm++;
    else if (item.status === 'error') serr++;
    else if (!item.scanned) sunscanned++;
  }
  const sum = document.getElementById('summary-bar');
  const parts = [];
  if (sren) parts.push(`🔄待改名 ${sren}`);
  if (snoop) parts.push(`✅无需改 ${snoop}`);
  if (sskip) parts.push(`⏭跳过 ${sskip}`);
  if (sunm) parts.push(`❓无匹配 ${sunm}`);
  if (serr) parts.push(`❌错误 ${serr}`);
  if (sunscanned) parts.push(`📋未扫描 ${sunscanned}`);
  if (parts.length) { sum.style.display='block'; sum.textContent = '📊 ' + parts.join(' | '); }
  else sum.style.display = 'none';
}

function renderGrid(items) {
  const grid = document.getElementById('folder-grid');
  const savedScroll = window.scrollY;
  grid.innerHTML = '';
  let count = 0;
  // summary
  let sren=0,snoop=0,sskip=0,sunm=0,serr=0,sunscanned=0;
  for (const item of items) {
    const card = renderCard(item);
    if (card) { grid.appendChild(card); count++; }
    // count all (not filtered)
    if (item.status === 'rename') sren++;
    else if (item.status === 'noop') snoop++;
    else if (item.status === 'skip') sskip++;
    else if (item.status === 'unmatched') sunm++;
    else if (item.status === 'error') serr++;
    else if (!item.scanned) sunscanned++;
  }
  document.getElementById('count-badge').textContent = `共 ${items.length} 个`;
  const sum = document.getElementById('summary-bar');
  const parts = [];
  if (sren) parts.push(`🔄待改名 ${sren}`);
  if (snoop) parts.push(`✅无需改 ${snoop}`);
  if (sskip) parts.push(`⏭跳过 ${sskip}`);
  if (sunm) parts.push(`❓无匹配 ${sunm}`);
  if (serr) parts.push(`❌错误 ${serr}`);
  if (sunscanned) parts.push(`📋未扫描 ${sunscanned}`);
  if (parts.length) { sum.style.display='block'; sum.textContent = '📊 ' + parts.join(' | '); }
  else sum.style.display = 'none';
  if (count === 0) {
    grid.innerHTML = '<div class="col-12"><div class="empty-state"><div class="icon">📁</div><p>没有匹配的文件夹</p></div></div>';
  }
  window.scrollTo(0, savedScroll);
}

async function refreshFolders() {
  const grid = document.getElementById('folder-grid');
  grid.innerHTML = '<div class="col-12 text-center text-muted py-5"><div class="spinner"></div>加载中...</div>';
  try {
    const r = await fetch('/api/folders');
    const d = await r.json();
    folders = d.items || [];
    if (selectedPaths.size === 0) {
      selectedPaths = new Set(folders.filter(f => f.status === 'rename').map(f => f.path));
    }
    renderGrid(folders);
  } catch(e) { showToast('加载失败: '+e.message, 'error'); }
}

function pollScanProgress() {
  scanPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/scan-status');
      const s = await r.json();
      const bar = document.getElementById('scan-progress-bar');
      const text = document.getElementById('scan-progress-text');
      if (s.total > 0) bar.style.width = Math.min(100, (s.current / s.total * 100)) + '%';
      text.textContent = s.running
        ? `扫描中: ${s.current}/${s.total} (${s.current_name||''})`
        : `扫描完成! ${s.total} 个`;
      document.getElementById('btn-scan-all').disabled = s.running;
      document.getElementById('btn-scan-incr').disabled = s.running;
      document.getElementById('btn-cancel-scan').style.display = s.running ? '' : 'none';

      if (!s.running) {
        clearInterval(scanPollTimer);
        scanPollTimer = null;
        // load results
        const rr = await fetch('/api/scan-results');
        const rd = await rr.json();
        if (rd.summary) {
          const sm = rd.summary;
          showToast(`扫描完成: rename=${sm.rename||0} noop=${sm.noop||0} skip=${sm.skip||0} unmatched=${sm.unmatched||0} error=${sm.error||0}`, rd.total > 0 ? 'success' : 'info');
        }
        await refreshFolders();
      }
    } catch(e) { /* ignore poll errors */ }
  }, 800);
}

async function startScan() {
  try {
    const r = await fetch('/api/scan-start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'full'})});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'warning'); return; }
    document.getElementById('progress-bar-wrap').style.display = 'block';
    document.getElementById('scan-progress-bar').style.width = '0%';
    document.getElementById('scan-progress-text').textContent = '全量扫描中...';
    pollScanProgress();
  } catch(e) { showToast('启动扫描失败: '+e.message, 'error'); }
}

async function startIncrementalScan() {
  try {
    const r = await fetch('/api/scan-incremental', {method:'POST'});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'warning'); return; }
    document.getElementById('progress-bar-wrap').style.display = 'block';
    document.getElementById('scan-progress-bar').style.width = '0%';
    document.getElementById('scan-progress-text').textContent = '增量扫描中（跳过已匹配）...';
    pollScanProgress();
  } catch(e) { showToast('启动增量扫描失败: '+e.message, 'error'); }
}

async function cancelScan() {
  await fetch('/api/scan-cancel', {method:'POST'});
  showToast('正在停止扫描...', 'warning');
}

async function scanOne(path, input) {
  const series_title = input ? input.value.trim() : '';
  showToast('🔍 正在扫描...', 'info');
  try {
    const r = await fetch('/api/scan-one', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path, series_title})});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    showToast(`✅ 扫描完成: ${d.series_title||path.split('/').pop()}`, 'success');
    // In-place update: don't re-render the whole grid
    const idx = folders.findIndex(f => f.path === path);
    if (idx >= 0) {
      folders[idx] = { ...folders[idx], ...d, scanned: true };
      if (folders[idx].status === 'rename') selectedPaths.add(path);
      else selectedPaths.delete(path);
      refreshSingleCard(folders[idx]);
      updateSummary();
    } else {
      await refreshFolders();
    }
  } catch(e) { showToast('扫描失败: '+e.message, 'error'); }
}

async function previewOne(path, input) {
  const series_title = input.value.trim();
  showToast('👁 正在加载预览...', 'info');
  try {
    const r = await fetch('/api/preview-one', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path, series_title})});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    const idx = folders.findIndex(f => f.path === path);
    if (idx >= 0) {
      folders[idx].operations = d.operations||[];
      folders[idx].series_title = d.series_title||series_title;
      folders[idx].status = d.status||'';
      folders[idx].top_candidate = d.top_candidate;
      folders[idx].scanned = true;
      if (folders[idx].status === 'rename') selectedPaths.add(path);
      else selectedPaths.delete(path);
      refreshSingleCard(folders[idx]);
      updateSummary();
    }
    showToast(`👁 预览完成: ${(d.operations||[]).length} 个操作`, 'success');
  } catch(e) { showToast('预览失败: '+e.message, 'error'); }
}

async function applyOne(path, input) {
  const series_title = input.value.trim();
  if (!confirm(`确定要重命名 "${path.split('/').pop()}" 吗？`)) return;
  showToast('✅ 正在执行改名...', 'info');
  try {
    const r = await fetch('/api/apply-one', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path, series_title})});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    showToast(`✅ 改名完成: 重命名 ${d.applied_renamed||0} 文件, ${d.applied_errors||0} 错误`, d.applied_errors?'warning':'success');
    await refreshFolders();
  } catch(e) { showToast('改名失败: '+e.message, 'error'); }
}

async function renameFolder(path, input) {
  const new_name = input.value.trim();
  if (!new_name) { showToast('请输入新名称', 'warning'); return; }
  if (!confirm(`确定要将文件夹重命名为 "${new_name}"？`)) return;
  showToast('📁 正在重命名文件夹...', 'info');
  try {
    const r = await fetch('/api/rename-folder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path, new_name})});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    showToast(`📁 文件夹已重命名为: ${d.new_name}`, 'success');
    await refreshFolders();
  } catch(e) { showToast('重命名失败: '+e.message, 'error'); }
}

async function applyAll() {
  const checked = Array.from(document.querySelectorAll('.folder-card input[type="checkbox"]:checked'));
  const paths = checked.map(box => box.closest('[data-path]').dataset.path);
  if (paths.length === 0) { showToast('请先勾选要批量应用的项目', 'warning'); return; }
  const overrides = {};
  for (const path of paths) {
    const card = document.querySelector(`[data-path="${CSS.escape(path)}"]`);
    const input = card ? card.querySelector('.folder-name-input') : null;
    if (input && input.value.trim()) overrides[path] = input.value.trim();
  }
  if (!confirm(`确定要应用 ${paths.length} 个已勾选项目的改名操作吗？`)) return;
  showToast('✅ 正在批量执行改名...', 'info');
  try {
    const r = await fetch('/api/apply-all', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paths, overrides})});
    const d = await r.json();
    if (d.error) { showToast(d.error, 'error'); return; }
    for (const path of paths) selectedPaths.delete(path);
    showToast(`全部完成: 重命名 ${d.total_renamed} 个, 错误 ${d.total_errors} 个`, d.total_errors?'warning':'success');
    await refreshFolders();
  } catch(e) { showToast('应用失败: '+e.message, 'error'); }
}

async function downloadNfo(path, input, btn) {
  const orig = btn.textContent;
  btn.textContent = '⏳ 下载中...';
  btn.disabled = true;
  showToast('📄 正在下载 NFO 元数据...', 'info');
  try {
    const r = await fetch('/api/download-nfo', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})});
    const d = await r.json();
    btn.textContent = orig; btn.disabled = false;
    if (d.error) { showToast(d.error, 'error'); return; }
    showToast(`📄 NFO 已下载: ${d.series_title} (${d.nfo_written}个文件)`, 'success');
    await refreshFolders();
  } catch(e) { btn.textContent = orig; btn.disabled = false; showToast('NFO 失败: '+e.message, 'error'); }
}

async function downloadPoster(path, btn) {
  const orig = btn.textContent;
  btn.textContent = '⏳ 下载中...';
  btn.disabled = true;
  showToast('🖼️ 正在下载海报...', 'info');
  try {
    const r = await fetch('/api/download-poster', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})});
    const d = await r.json();
    btn.textContent = orig; btn.disabled = false;
    if (d.error) { showToast(d.error, 'error'); return; }
    if (d.note === '已存在') showToast('🖼️ 海报已存在', 'info');
    else showToast(`🖼️ 海报已下载: ${d.count||0} 个文件`, 'success');
    await refreshFolders();
  } catch(e) { btn.textContent = orig; btn.disabled = false; showToast('海报下载失败: '+e.message, 'error'); }
}

async function downloadAllNfo() {
  const candidates = folders.filter(f => f.top_candidate);
  if (candidates.length === 0) { showToast('没有已匹配的文件夹，请先扫描', 'warning'); return; }
  if (!confirm(`确定要为 ${candidates.length} 个已匹配文件夹下载 NFO 吗？`)) return;
  const btn = document.getElementById('btn-download-all-nfo');
  const orig = btn.textContent;
  let ok=0, fail=0, i=0;
  for (const item of candidates) {
    btn.textContent = `⏳ NFO ${++i}/${candidates.length}`;
    try {
      const r = await fetch('/api/download-nfo', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:item.path})});
      const d = await r.json();
      if (d.error) { showToast(`${item.name}: ${d.error}`, 'warning'); fail++; }
      else ok++;
    } catch(e) { showToast(`${item.name}: ${e.message}`, 'error'); fail++; }
  }
  btn.textContent = orig;
  showToast(`📄 NFO 批量完成: ${ok} 成功, ${fail} 失败`, fail?'warning':'success');
  await refreshFolders();
}

async function downloadAllPosters() {
  const candidates = folders.filter(f => f.top_candidate);
  if (candidates.length === 0) { showToast('没有已匹配的文件夹，请先扫描', 'warning'); return; }
  if (!confirm(`确定要为 ${candidates.length} 个已匹配文件夹下载海报吗？`)) return;
  const btn = document.getElementById('btn-download-all-posters');
  const orig = btn.textContent;
  let ok=0, fail=0, skip=0, i=0;
  for (const item of candidates) {
    btn.textContent = `⏳ 海报 ${++i}/${candidates.length}`;
    try {
      const r = await fetch('/api/download-poster', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:item.path})});
      const d = await r.json();
      if (d.error) { showToast(`${item.name}: ${d.error}`, 'warning'); fail++; }
      else if (d.note === '已存在') skip++;
      else ok++;
    } catch(e) { showToast(`${item.name}: ${e.message}`, 'error'); fail++; }
  }
  btn.textContent = orig;
  showToast(`🖼️ 海报批量完成: ${ok} 下载, ${skip} 已有, ${fail} 失败`, fail?'warning':'success');
  await refreshFolders();
}

function filterCards() {
  searchTerm = document.getElementById('search-input').value;
  renderGrid(folders);
}

function setFilter(btn, f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btns .btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderGrid(folders);
}

document.addEventListener('DOMContentLoaded', refreshFolders);
</script>
</body>
</html>"""


# ── main ───────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="里番重命名 WebUI")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=5800, help="监听端口")
    parser.add_argument("--root", default=str(APP_DIR), help="媒体库根目录（默认: 代码所在目录）")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    args = parser.parse_args()

    global MEDIA_ROOT
    MEDIA_ROOT = Path(args.root).resolve()

    print(f"🎬 里番重命名 WebUI")
    print(f"   地址: http://{args.host}:{args.port}")
    print(f"   目录: {MEDIA_ROOT}")
    print(f"   TMDB: {'✅ 已配置' if _api_key() else '❌ 未配置'}")
    print(f"   AniDB缓存: {'✅ 存在' if ANIDB_CACHE.exists() else '❌ 未找到'}")
    print(f"   按 Ctrl+C 停止")

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
从扫描缓存创建 Emby 兼容的硬链接目录结构。

对每个已扫描的系列：
  Output/SeriesName/Season 1/file.mkv
  Output/SeriesName/Season 2/file.mkv
  Output/SeriesName/Specials/file.mkv

用法:
  python3 emby_link.py --output /path/to/emby/library --dry-run
  python3 emby_link.py --output /path/to/emby/library --apply
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".webm"}


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def parse_se_episode(filename: str) -> tuple[int, int] | None:
    """从文件名提取 (season, episode)。支持 S01E02 和 E02 格式。"""
    m = re.search(r"S(\d+)E(\d+)", filename, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"E(\d+)", filename, re.IGNORECASE)
    if m:
        return 1, int(m.group(1))
    return None


def season_dir_name(season: int) -> str:
    return "Specials" if season == 0 else f"Season {season}"


def link_file(src: Path, dst: Path) -> bool:
    """硬链接文件，跨设备时回退到复制。返回 True 表示成功。"""
    if dst.exists():
        return "skip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return True
    except OSError:
        try:
            import shutil
            shutil.copy2(src, dst)
            return True
        except OSError as e:
            return False


def process_entry(entry: dict, output_root: Path, path_prefix_old: str, path_prefix_new: str, dry_run: bool):
    """处理单个系列条目，返回 (created, skipped, errors) 计数。

    path_prefix_old: 缓存中的路径前缀 (如 /mnt/media/里番/)
    path_prefix_new: 实际文件系统的路径前缀 (如 /tank/里番/)
    """
    created = skipped = errors = 0

    def remap(p: str) -> Path:
        if path_prefix_new:
            parts = Path(p).parts
            try:
                idx = parts.index("里番")
                relative = Path(*parts[idx:])
                return Path(path_prefix_new.rstrip("/")) / relative
            except ValueError:
                pass
            if path_prefix_old and p.startswith(path_prefix_old):
                return Path(path_prefix_new + p[len(path_prefix_old):])
        return Path(p)

    status = entry.get("status", "")
    if status not in ("noop", "rename"):
        return 0, 0, 0

    if entry.get("name", "").startswith("."):
        return 0, 0, 0

    series_title = sanitize(entry.get("series_title") or entry.get("name", "Unknown"))
    source_path = entry.get("path", "")
    operations = entry.get("operations", [])

    if not source_path:
        return 0, 0, 0

    src_dir = remap(source_path)
    if not src_dir.is_dir():
        return 0, 0, 0

    if operations:
        for op in operations:
            status = op.get("status", "")
            if status not in ("noop", "rename", "skip"):
                continue

            src_file = remap(op["path"])
            src_name = src_file.name

            se = None
            if op.get("target"):
                se = parse_se_episode(Path(op["target"]).name)
            if not se:
                se = parse_se_episode(src_name)
            if not se:
                continue

            s, e = se
            sdir = season_dir_name(s)
            dst = output_root / series_title / sdir / src_name

            if dry_run:
                print(f"  → {output_root.name}/{series_title}/{sdir}/{src_file.name}")
                created += 1
            else:
                result = link_file(src_file, dst)
                if result is True:
                    created += 1
                elif result == "skip":
                    skipped += 1
                else:
                    errors += 1

    # ── 情况 2: 无操作记录，遍历目录重建 ──
    else:
        subdirs = sorted(
            [d for d in src_dir.iterdir()
             if d.is_dir() and not d.name.startswith(".")],
            key=lambda d: d.name.lower()
        )

        season_pat = re.compile(r"^(?:S(?:eason\s*)?)?(\d{1,2})$", re.IGNORECASE)
        ep_subdir_pat = re.compile(r"^(\d{1,3})(?:[\s._-]+.*)?$")

        processed_files = 0

        if subdirs:
            for sub in subdirs:
                s = 1
                sm = season_pat.match(sub.name)
                if sm:
                    s = int(sm.group(1))
                else:
                    em = ep_subdir_pat.match(sub.name)
                    if not em:
                        continue

                sdir = season_dir_name(s)

                videos = sorted(
                    [f for f in sub.iterdir()
                     if f.suffix.lower() in MEDIA_EXTS],
                    key=lambda f: f.name.lower()
                )
                for vf in videos:
                    se = parse_se_episode(vf.name)
                    actual_s = se[0] if se else s
                    actual_sdir = season_dir_name(actual_s)

                    dst = output_root / series_title / actual_sdir / vf.name

                    if dry_run:
                        print(f"  → {output_root.name}/{series_title}/{actual_sdir}/{vf.name}")
                        created += 1
                    else:
                        result = link_file(vf, dst)
                        if result is True:
                            created += 1
                        elif result == "skip":
                            skipped += 1
                        else:
                            errors += 1
                    processed_files += 1

        # 根目录下的视频文件（扁平结构或混合结构）
        root_videos = sorted(
            [f for f in src_dir.iterdir()
             if f.is_file() and f.suffix.lower() in MEDIA_EXTS],
            key=lambda f: f.name.lower()
        )
        if processed_files == 0:
            for vf in root_videos:
                se = parse_se_episode(vf.name)
                s = se[0] if se else 1
                sdir = season_dir_name(s)

                dst = output_root / series_title / sdir / vf.name

                if dry_run:
                    print(f"  → {output_root.name}/{series_title}/{sdir}/{vf.name}")
                    created += 1
                else:
                    result = link_file(vf, dst)
                    if result is True:
                        created += 1
                    elif result == "skip":
                        skipped += 1
                    else:
                        errors += 1

    return created, skipped, errors


def main():
    parser = argparse.ArgumentParser(description="创建 Emby 兼容的硬链接目录结构")
    parser.add_argument("--cache", default="webui_scan_cache.json",
                        help="扫描缓存 JSON 路径 (默认: webui_scan_cache.json)")
    parser.add_argument("--output", required=True,
                        help="Emby 库输出根目录")
    parser.add_argument("--cache-prefix", default="/mnt/media/里番/",
                        help="缓存中路径的前缀 (默认: /mnt/media/里番/)")
    parser.add_argument("--actual-prefix", default="/tank/里番/",
                        help="实际文件系统的路径前缀 (默认: /tank/里番/)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="仅预览，不实际创建链接 (默认)")
    parser.add_argument("--apply", dest="dry_run", action="store_false",
                        help="实际执行创建")
    args = parser.parse_args()

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"错误: 缓存文件不存在: {cache_path}")
        sys.exit(1)

    with open(cache_path) as f:
        cache = json.load(f)

    output_root = Path(args.output)

    total_created = total_skipped = total_errors = 0
    series_count = 0

    for key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status", "")
        if status not in ("noop", "rename"):
            continue

        series_count += 1
        series_title = sanitize(entry.get("series_title") or entry.get("name", "?"))

        if args.dry_run:
            print(f"\n📁 {series_title}")

        c, s, e = process_entry(entry, output_root,
                                args.cache_prefix, args.actual_prefix,
                                args.dry_run)
        total_created += c
        total_skipped += s
        total_errors += e

    print(f"\n{'='*50}")
    print(f"{'[DRY RUN] ' if args.dry_run else ''}完成:")
    print(f"  系列数: {series_count}")
    print(f"  文件: {total_created} 新建, {total_skipped} 跳过, {total_errors} 错误")

    if args.dry_run:
        print(f"\n确认无误后运行:")
        print(f"  python3 emby_link.py --output {args.output} --apply")


if __name__ == "__main__":
    main()

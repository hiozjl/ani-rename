#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any


MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".srt", ".ass", ".ssa", ".sub"}


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def path_remap(value: str, mappings: list[tuple[str, str]]) -> str:
    for old, new in mappings:
        if value.startswith(old):
            return new + value[len(old) :]
    return value


def parse_remaps(remap_args: list[str]) -> list[tuple[str, str]]:
    result = []
    for item in remap_args:
        if "=" not in item:
            raise SystemExit(f"Invalid --path-remap value: {item}")
        left, right = item.split("=", 1)
        result.append((left, right))
    return result


def chosen_series_title(item: dict[str, Any], source: str) -> str:
    local = item.get("title_hint", "").strip()
    top = (item.get("candidates") or [{}])[0]
    tmdb = (top.get("name") or "").strip()
    if source == "tmdb" and tmdb:
        return tmdb
    return local or tmdb


def rename_suffix(proposed_name: str) -> str:
    match = re.match(r"^.*?( - S\d{2}E\d{2}(?:\.5)?(?: - .*)?\.[^.]+)$", proposed_name)
    if match:
        return match.group(1)
    return proposed_name


def build_plan_for_item(item: dict[str, Any], series_title_source: str, remaps: list[tuple[str, str]]) -> list[dict[str, str]]:
    if item.get("structure") != "episode_subdirs":
        return []
    root = Path(path_remap(item["path"], remaps))
    if not root.exists():
        raise SystemExit(f"Series path not found after remap: {root}")

    title = chosen_series_title(item, series_title_source)
    plans: list[dict[str, str]] = []
    for episode in item.get("episodes", []):
        episode_dir = root / episode["episode_dir"]
        proposed_names = episode.get("proposed_file_names", [])
        media_files = episode.get("media_files", [])
        if len(proposed_names) != len(media_files):
            raise SystemExit(f"Manifest mismatch in {episode_dir}")
        for source_name, proposed_name in zip(media_files, proposed_names, strict=True):
            old_path = episode_dir / source_name
            if old_path.suffix.lower() not in MEDIA_EXTS:
                continue
            suffix = rename_suffix(proposed_name)
            new_name = f"{title}{suffix}" if suffix.startswith(" - ") else proposed_name
            new_path = episode_dir / new_name
            if new_path == old_path:
                continue
            plans.append({"old_path": str(old_path), "new_path": str(new_path)})
    return plans


def approve_items(items: list[dict[str, Any]], approve_paths: list[str], all_medium_or_higher: bool) -> list[dict[str, Any]]:
    approved = []
    approve_set = set(approve_paths)
    for item in items:
        if item["path"] in approve_set:
            approved.append(item)
            continue
        if all_medium_or_higher:
            if item.get("recommended_action") == "eligible_for_batch_approval":
                approved.append(item)
                continue
            if item.get("top_candidate_band") == "medium" and not item.get("reason_flags"):
                approved.append(item)
    return approved


def validate_plans(plans: list[dict[str, str]]) -> None:
    filtered = []
    targets = []
    for plan in plans:
        old_path = Path(plan["old_path"])
        new_path = Path(plan["new_path"])
        if not old_path.exists() and new_path.exists():
            continue
        filtered.append(plan)
        targets.append(plan["new_path"])
    if len(targets) != len(set(targets)):
        raise SystemExit("Duplicate target paths detected")
    plans[:] = filtered
    for plan in plans:
        old_path = Path(plan["old_path"])
        new_path = Path(plan["new_path"])
        if not old_path.exists():
            raise SystemExit(f"Missing source file: {old_path}")
        if new_path.exists() and new_path != old_path:
            raise SystemExit(f"Target already exists: {new_path}")


def apply_plans(plans: list[dict[str, str]], dry_run: bool) -> None:
    for plan in plans:
        print(f"PLAN: {plan['old_path']} -> {plan['new_path']}")
    if dry_run:
        return
    for plan in plans:
        Path(plan["old_path"]).rename(Path(plan["new_path"]))
        print(f"RENAMED: {plan['old_path']} -> {plan['new_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply approved renames from a TMDB preview manifest.")
    parser.add_argument("--manifest", required=True, help="Path to preview manifest JSON")
    parser.add_argument("--approve-path", action="append", default=[], help="Series path from manifest to approve")
    parser.add_argument("--all-medium-or-higher", action="store_true", help="Approve items with no flags and medium/high candidate band")
    parser.add_argument("--series-title-source", choices=["local", "tmdb"], default="local", help="Whether to use local folder name or top TMDB candidate as series title")
    parser.add_argument("--path-remap", action="append", default=[], help="Rewrite manifest path prefix for testing, e.g. /src=/tmp/test")
    parser.add_argument("--transaction-log", default="tmdb_apply_log.json", help="Where to write plan/apply log")
    parser.add_argument("--apply", action="store_true", help="Actually perform renames; otherwise dry-run")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    remaps = parse_remaps(args.path_remap)
    approved = approve_items(manifest.get("items", []), args.approve_path, args.all_medium_or_higher)
    if not approved:
        raise SystemExit("No approved items selected")

    plans: list[dict[str, str]] = []
    for item in approved:
        plans.extend(build_plan_for_item(item, args.series_title_source, remaps))
    validate_plans(plans)
    apply_plans(plans, dry_run=not args.apply)

    log_payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": not args.apply,
        "manifest": str(manifest_path),
        "series_title_source": args.series_title_source,
        "approved_paths": [item["path"] for item in approved],
        "path_remap": remaps,
        "plans": plans,
    }
    Path(args.transaction_log).resolve().write_text(json.dumps(log_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote transaction log: {Path(args.transaction_log).resolve()}")
    print(f"Planned renames: {len(plans)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fetch the public Toutiao hot board once; no login or external dependencies."""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", help="comma-separated manually selected cluster IDs; default: first 10 unique entries for review")
    args = parser.parse_args()
    selected = set(args.ids.split(",")) if args.ids else None
    start = time.monotonic()
    sampled = dt.datetime.now(TZ)
    out = ROOT / "runs" / sampled.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    stem = sampled.strftime("%H%M%S_%f")
    result = {"board": "今日头条热榜", "board_url": URL,
              "sampled_at": sampled.isoformat(timespec="seconds"),
              "source_time": None, "source_time_note": "接口未提供可核验榜单更新时间",
              "selection": "人工指定话题ID" if selected else "接口返回顺序前10条，未经人工适宜性筛选",
              "http_status": None, "error": None, "response": None, "candidates": []}
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            result["http_status"] = response.status
            result["response"] = json.loads(response.read().decode("utf-8"))
        payload = result["response"]
        if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(payload.get("data"), list):
            raise ValueError("接口状态或 data 结构异常")
        seen = set()
        for order, item in enumerate(payload["data"], 1):
            title = str(item.get("Title") or "").strip()
            cluster_id = str(item.get("ClusterIdStr") or item.get("ClusterId") or "")
            if not title or (selected and cluster_id not in selected):
                continue
            key = hashlib.sha256(re.sub(r"\s+", "", title).casefold().encode("utf-8")).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            result["candidates"].append({"title": title, "source_order": order,
                "cluster_id": cluster_id, "topic_url": item.get("Url"),
                "hot_value_raw": item.get("HotValue"), "dedup_key": key,
                "article_url": None, "publication_time": None, "event_time": None,
                "content_status": "仅榜单标题线索", "angle": None})
            if len(result["candidates"]) == 10:
                break
        if selected:
            result["missing_selected_ids"] = sorted(selected - {c["cluster_id"] for c in result["candidates"]})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    snapshot = out / f"{stem}_头条热榜.json"
    draft = out / f"{stem}_候选待核.md"
    snapshot.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 今日头条热榜候选初稿", "", f"采样：{result['sampled_at']}；耗时 {result['elapsed_seconds']} 秒；来源：{URL}",
        f"选择方式：{result['selection']}。source_order 为接口 data 顺序，并非已核验榜单名次。",
        "HotValue 为平台原始热度字段，具体计算口径未知，不是阅读量。榜单更新时间、事件时间均未提供。",
        "仅标题线索，事实正文、适宜性及加工角度需要人工核验；未自动发布。", ""]
    if result["error"]:
        lines += [f"抓取失败：{result['error']}", ""]
    for c in result["candidates"]:
        lines += [f"## {c['source_order']}. {c['title']}", "",
            f"- 榜单话题：{c['topic_url']}", f"- HotValue 原始值：{c['hot_value_raw']}",
            f"- 去重键：{c['dedup_key']}；事件时间、正文链接：未知；状态：仅标题线索。", ""]
    draft.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"snapshot": str(snapshot), "draft": str(draft), "sampled_at": result["sampled_at"],
        "elapsed_seconds": result["elapsed_seconds"], "count": len(result["candidates"]),
        "missing_selected_ids": result.get("missing_selected_ids", []), "error": result["error"]}, ensure_ascii=False))
    return 1 if result["error"] else 0


if __name__ == "__main__":
    sys.exit(main())

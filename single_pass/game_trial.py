"""Compare the first six game sources with all twelve on one frozen local collection.

python -m single_pass.game_trial --root runs/NAME --date YYYY-MM-DD
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from threading import Lock
import time
import uuid

from . import collect, game_sources, materials, pool, screen, screening_scope
from .production import ProductionService
from .server import App
from .workspace import Workspace, read


BASE = (
    "game_3dm_news",
    "game_ps_blog",
    "game_steam_new",
    "game_hykb_calendar",
    "game_indienova_logs",
    "game_github_tools",
)
ALL = (
    *BASE,
    "game_nintendo_hk",
    "game_xbox_wire",
    "game_gcores",
    "game_ark_official",
    "game_indienova_news",
    "game_godot_tools",
)
LINES = ("game_digest", "game_pick", "game_devlog")


class FrozenReader:
    def __init__(self, directory):
        self.directory = directory
        self.background_library = directory.parent / "game-background.sqlite3"
        directory.mkdir(exist_ok=True)
        self.lock = Lock()
        self.locks = {}

    def __call__(self, item, token):
        # The same original in either arm gets exactly the same bytes and fetch outcome.
        key = hashlib.sha256(item["entry_url"].encode()).hexdigest()
        with self.lock:
            lock = self.locks.setdefault(key, Lock())
        with lock:
            path = self.directory / (key + ".json")
            cached = read(path)
            if cached is None:
                row, original = materials.resolve_original(item, token)
                cached = {
                    "resolution": {
                        k: row.get(k) for k in ("original_url", "original_status", "resolution_evidence")
                    },
                    "original": original,
                }
                screen.write_json(path, cached)
            value = cached["original"]
            if isinstance(value, list):
                value = [dict(m, origin_ref=[item["ref"]]) for m in value]
            elif value:
                value = dict(value, origin_ref=[item["ref"]])
            return dict(item, **cached["resolution"]), value


def freeze(root, day):
    path = root / "window.json"
    if path.exists():
        window = read(path)
        if window["day"] != day:
            raise ValueError("这个试验目录已经冻结在其他日期")
        return window
    ws = Workspace(root / "live", None)
    if not ws.collection.get("id"):
        ws.start_collect(source_ids=list(ALL))
        ws.collection_thread.join()
    sources = {}
    for result in sorted((ws.data / "collections").glob("*/result.json")):
        batch = read(result)
        for source in batch["sources"]:
            source_id = source["source_id"]
            sources[source_id] = dict(
                source,
                collection_id=batch["id"],
                pages_path=str(result.parent / source_id / "pages.json"),
            )
    if set(ALL) - sources.keys():
        raise ValueError("尚未采完 12 个来源")
    with ws.database.connect() as db:
        values = [
            json.loads(r["payload"]) for r in db.execute("SELECT payload FROM pool_items ORDER BY id")
        ]
    rows = [
        x
        for x in values
        if x["source_id"] in ALL
        and pool.effective_time(x)
        and pool.effective_time(x).astimezone(collect.TZ).date().isoformat() == day
    ]
    window = {
        "day": day,
        "timezone": "Asia/Shanghai",
        "sources": sources,
        "rows": rows,
        "frozen_at": collect.now(),
        "raw_count": len(values),
        "window_counts": dict(Counter(x["source_id"] for x in rows)),
    }
    screen.write_json(path, window)
    return window


def launch(root, window, name, allow, reader, lines=LINES):
    w = Workspace(root / name, App, original_reader=reader)
    config = w.config()
    for line in config["lines"]:
        if line["id"] in LINES:
            line["sources"] = [s for s in line["sources"] if s in allow]
            line["auto_draft"] = name == "all12"
            line["targets"] = []
    screen.write_json(w.column_config.path, config)
    w.production = ProductionService(w.data, w.candidates) if name == "all12" else None
    rows = [r for r in window["rows"] if r["source_id"] in allow]
    if not w.latest:
        for r in rows:
            w.database.ingest([r], r["last_seen_at"])
    w.collection = {"status": "completed", "sources": [window["sources"][s] for s in allow]}
    # All column batches in this arm belong to the same frozen trial invocation.
    record_id = (
        next(
            (
                read(w.data / w.latest[key] / "batch.json")["topic_record_id"]
                for key in lines
                if key in w.latest
            ),
            None,
        )
        or uuid.uuid4().hex
    )
    for key in lines:
        prior = w.latest.get(key)
        if prior and read(w.data / prior / "batch.json")["status"] == "completed":
            if w.production:
                record = w.draft_selection.rounds.get(prior)
                if record and record["status"] == "failed":
                    w.draft_selection.edit(
                        w.line(key),
                        {"action": "retry", "batch_id": prior, "revision": record["revision"]},
                    )
                else:
                    w.draft_selection.prepare(w.line(key), prior)
            continue
        if prior:
            w.start_screen(batch_id=prior, line_id=key, runtime_checked=True)
            continue
        day = window["day"]
        spec = {
            "line_id": key,
            "hours": "custom",
            "start": day + "T00:00:00+08:00",
            "end": day + "T23:59:59.999999+08:00",
        }
        plan = screening_scope.resolve(w, spec)
        if plan["items"]:
            # Membership is frozen. Later replay must never age these records out.
            plan["scope"]["effective_hours"] = None
            plan["evidence_cutoff_at"] = window["frozen_at"]
            w.start_screen(line_id=key, prepared=plan, runtime_checked=True, topic_record_id=record_id)
    return w


def replay(root, source_root, day):
    """Reparse archived pages after a reader change, never silently fetch new list data."""
    source_root = source_root.resolve()
    old = read(source_root / "window.json")
    if old["day"] != day:
        raise ValueError("冻结窗口日期不匹配")
    ws = Workspace(root / "live", None)
    replayed = {}
    for source in ALL:
        result = old["sources"][source]
        directory = (
            Path(result["pages_path"]).parent
            if result.get("pages_path")
            else source_root / "live" / "collections" / result["collection_id"] / source
        )
        manifest = read(directory / "pages.json")

        def archived(url):
            page = manifest["pages"][url]
            if "error" in page:
                raise OSError(page["error"])
            data = (directory / page["file"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != page["sha256"]:
                raise ValueError("归档列表哈希不符")
            return data.decode("utf-8")

        archived.archived_urls = manifest["pages"].keys()
        rows = game_sources.collect_rows(source, manifest["observed_at"], archived)
        unique = game_sources.unique_rows(rows)
        for row in unique:
            row["snapshot_ref"] = str(directory / "pages.json")
        ws.database.ingest(unique, manifest["observed_at"])
        replayed[source] = dict(result, count=len(unique), pages_path=str(directory / "pages.json"))
    with ws.database.connect() as db:
        items = [
            json.loads(r["payload"]) for r in db.execute("SELECT payload FROM pool_items ORDER BY id")
        ]
    selected = [
        x
        for x in items
        if pool.effective_time(x)
        and pool.effective_time(x).astimezone(collect.TZ).date().isoformat() == day
    ]
    window = dict(
        old,
        rows=selected,
        raw_count=len(items),
        window_counts=dict(Counter(x["source_id"] for x in selected)),
        replay_of=str(source_root),
        sources=replayed,
        frozen_at=old["frozen_at"],
    )
    screen.write_json(root / "window.json", window)
    return window


def export_result(root, output, lines):
    from . import composition, editorial, review

    output.mkdir(parents=True, exist_ok=True)
    window = read(root / "window.json")
    result = {"day": window["day"], "window": str(root / "window.json"), "columns": {}, "drafts": []}
    for arm, allow in [("base6", BASE), ("all12", ALL)]:
        current = read(root / arm / "columns-current.json", {})
        for key in lines:
            if key not in current:
                continue
            batch = root / arm / current[key]
            state = read(batch / "review/state.json")
            items = {i["ref"]: i for i in read(batch / "items.json")}
            events = state["events"]
            selected = []
            for event in events:
                if not (event.get("comparison") or {}).get("selected"):
                    continue
                sources = {
                    m["material_id"]: review.material_content(batch / "review", m)
                    for m in event["materials"]
                    if m["status"] == "read"
                }
                facts = (event.get("assessment") or {}).get("facts", [])
                if not facts or any(
                    not materials.verify_material_quote(sources[f["material_id"]], f["quote"])
                    for f in facts
                ):
                    raise ValueError("可写题缺少可定位的核实引文：" + event["id"])
                story = {
                    "id": event["id"],
                    "title": items[event["id"]]["title"],
                    "items": [
                        {
                            k: items[r].get(k)
                            for k in ("ref", "source_id", "title", "entry_url", "snapshot_ref")
                        }
                        for r in event["refs"]
                    ],
                    "reason": event["comparison"]["reason"],
                    "quotes": facts,
                }
                if key == "game_pick":
                    background = review.material_content(batch / "review", event["pick_background"])
                    if not materials.verify_quote(background["content"], background["background_quote"]):
                        raise ValueError("副文背景引文丢失")
                    story["background"] = {
                        k: background[k]
                        for k in (
                            "url",
                            "subject",
                            "angle",
                            "main_quote",
                            "background_quote",
                            "content_sha256",
                        )
                    }
                selected.append(story)
            result["columns"].setdefault(key, {})[arm] = {
                "status": read(batch / "batch.json")["status"],
                "batch": str(batch),
                "inputs": len(items),
                "events": len(events),
                "fetched_events": sum(
                    any(m.get("status") == "read" for m in e.get("materials", [])) for e in events
                ),
                "writable": len(selected),
                "stories": selected,
            }
    for path in (root / "all12" / "production" / "jobs").glob("*.json"):
        job = read(path)
        line = job["packet"]["line"]
        if (
            line["id"] not in lines
            or job["target"]["platform"] != "local"
            or job.get("remote")
            or not editorial.approved(job)
        ):
            continue
        draft = job["draft"]
        dest = output / (line["name"] + ".md")
        if any(item["column"] == line["name"] for item in result["drafts"]):
            dest = output / (line["name"] + "-" + job["id"][:8] + ".md")
        markdown = draft["markdown"].strip()
        approved = composition.semantic_html(draft["body_html"])
        if composition.semantic_html(composition.import_markdown(markdown)) != approved:
            # Final typesetting can split a paragraph after the writer's Markdown was saved.
            paragraphs = "\n\n".join(approved["paragraphs"])
            markdown = (
                paragraphs
                if composition.semantic_html(composition.import_markdown(paragraphs)) == approved
                else draft["body_html"]
            )
        dest.write_text("# " + draft["title"] + "\n\n" + markdown + "\n", encoding="utf-8")
        sources = [
            {k: m.get(k) for k in ("role", "usage", "subject", "title", "url", "content_sha256")}
            for m in job["packet"]["materials"]
            if m["status"] == "read"
        ]
        result["drafts"].append(
            {
                "column": line["name"],
                "title": draft["title"],
                "file": str(dest),
                "job": str(path),
                "source_hash": job["editorial_review"]["source_hash"],
                "review": "pass",
                "sources": sources,
            }
        )
    with pool.Pool(root / "live" / "pool.sqlite3").connect() as db:
        listed = dict(db.execute("SELECT source_id,COUNT(*) FROM pool_items GROUP BY source_id"))
    result["sources"] = {
        key: {
            "name": collect.SOURCES[key],
            "listed": listed.get(key, 0),
            "in_window": window["window_counts"].get(key, 0),
            "errors": window["sources"][key].get("page_errors", []),
            "collection_id": window["sources"][key]["collection_id"],
            "error": window["sources"][key].get("error"),
        }
        for key in ALL
    }
    screen.write_json(output / "对照明细.json", result)
    names = {line["id"]: line["name"] for line in read(Path(__file__).with_name("game_columns.json"))}
    lines_out = [
        f"# {window['day']} 游戏渠道对照",
        "",
        "按北京时间的完整自然日取数。两组共用列表快照、相同的栏目规则和取材预算。相同详情链接在两组复用。列表只有日期的，按来源标出的日期归窗。发售日、日历排期和发文日分别保留。",
        "",
        "| 本期实测 | 6 渠道 | 12 渠道 |",
        "|---|---:|---:|",
        f"| 日期窗口内的去重线索 | {sum(window['window_counts'].get(s, 0) for s in BASE)} | {len(window['rows'])} |",
    ]
    for key, arms in result["columns"].items():

        def count(arm):
            row = arms.get(arm)
            return row["writable"] if row and row["status"] == "completed" else "未完成"

        lines_out.append(f"| {names[key]}可写题 | {count('base6')} | {count('all12')} |")
    lines_out.extend(
        [
            "",
            "每个栏目单独归组。速读按可独立成段的消息计数，新作须有主推原文和可逐字核对的背景，日志须有能成立的手记或工具题。只统计本轮在预算内实际核过并晋级的题；成稿另计。",
            "",
            "## 成稿与出处",
            "",
        ]
    )
    for draft in result["drafts"]:
        links = " · ".join(
            f"[{m['subject'] or m['title'] or '来源正文'}]({m['url']})" for m in draft["sources"]
        )
        lines_out.append(
            f"- {draft['column']} · [{draft['title']}]({Path(draft['file']).name})。{links}"
        )
    lines_out.extend(["", "## 两组分别选出了什么", ""])
    for key, arms in result["columns"].items():
        lines_out.append(f"**{names[key]}**")
        for label, arm in (("6 渠道", "base6"), ("12 渠道", "all12")):
            stories = arms.get(arm, {}).get("stories", [])
            links = "、".join(f"[{s['title']}]({s['items'][0]['entry_url']})" for s in stories)
            lines_out.append(f"- {label}：{links or '本轮无已核实题目'}")
        lines_out.append("")
    gaps = [v["name"] for v in result["sources"].values() if v.get("error") or v.get("errors")]
    if gaps:
        lines_out.append("有页面读取失败的渠道：" + "、".join(gaps) + "。缺失部分未计入。")
    lines_out.append("逐题引文、材料哈希与审稿记录见[对照明细](对照明细.json)。")
    (output / "同窗对照.md").write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--replay-from", type=Path)
    parser.add_argument(
        "--originals-from",
        type=Path,
        help="Reuse details already read for both arms of an earlier replay",
    )
    parser.add_argument("--lines", choices=LINES, nargs="+", default=LINES)
    parser.add_argument(
        "--output", type=Path, help="Directory for accepted Markdown drafts and evidence"
    )
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root / "成稿").resolve()
    if args.export_only:
        if read(root / "window.json")["day"] != args.date.isoformat():
            raise ValueError("导出日期与冻结窗口不符")
        export_result(root, output, args.lines)
        return
    root.mkdir(parents=True, exist_ok=True)
    window = (
        replay(root, args.replay_from, args.date.isoformat())
        if args.replay_from and not (root / "window.json").exists()
        else freeze(root, args.date.isoformat())
    )
    print("时间窗", window["window_counts"], flush=True)
    original_root = args.originals_from.resolve() if args.originals_from else root
    if read(original_root / "window.json")["day"] != window["day"]:
        raise ValueError("正文快照与试验时间窗不符")
    reader = FrozenReader(original_root / "originals")
    workspaces = [
        launch(root, window, name, allow, reader, args.lines)
        for name, allow in [("all12", ALL), ("base6", BASE)]
    ]
    for w in workspaces:
        for future, _ in list(w.workers.values()):
            future.result()
    w = workspaces[0]
    while True:
        for key in args.lines:
            w.draft_selection.dispatch(w.line(key))
        if (
            not w.draft_selection.active
            and not w.production.running
            and not any(
                r["status"] in ("judging", "ready", "replenishing")
                for r in w.draft_selection.rounds.values()
            )
        ):
            break
        time.sleep(2)
    for workspace in workspaces:
        print(
            workspace.data,
            {k: read(workspace.data / v / "batch.json")["status"] for k, v in workspace.latest.items()},
            flush=True,
        )
        workspace.shutdown()
    w.production.shutdown()
    export_result(root, output, args.lines)


if __name__ == "__main__":
    main()

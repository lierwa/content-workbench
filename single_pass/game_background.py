"""Reusable, attributed gameplay comparisons for a single new release."""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import re
import unicodedata
from urllib.parse import urlsplit

from . import collect, materials, review
from .game_sources import stamp
from .pool import instant


# Tag matches discover a source to read. Only a checked quotation can enter an article.
REFERENCES = (
    ("2459550", "余烬守卫", ("塔防", "守城", "防御塔")),
    ("1059990", "Trombone Champ", ("节奏", "音乐", "钢琴", "音符", "乐器")),
    ("736260", "Baba Is You", ("解谜", "谜题", "规则", "推箱子")),
    ("246900", "Viscera Cleanup Detail", ("清洁", "打扫", "清洗", "血迹", "清理")),
    ("1167630", "Teardown", ("破坏", "拆除", "体素", "物理沙盒")),
    ("413150", "Stardew Valley", ("农场", "种田", "钓鱼", "农业")),
    ("646570", "Slay the Spire", ("卡牌", "肉鸽", "牌组", "卡组", "rogue")),
    ("1794680", "吸血鬼幸存者", ("幸存者", "尸潮", "弹幕", "自动攻击")),
    ("504230", "Celeste", ("平台跳跃", "平台动作", "冲刺", "横版", "平台游戏")),
    ("206190", "Gunpoint", ("潜行", "潜入", "解谜", "机关")),
    ("221910", "The Stanley Parable", ("选择", "叙事", "黑色幽默", "多结局", "文字冒险")),
    ("1145360", "Hades", ("动作", "地牢", "战斗", "动作冒险", "rogue")),
    ("1206560", "WorldBox", ("沙盒", "模拟经营", "策略", "建造")),
)


class MissingMaterial(ValueError):
    """This story requires more source evidence before the writer may start."""


def require_pick_gallery(sources, packet):
    if packet["line"].get("format_policy", {}).get("body_images", "none") == "none":
        return
    originals = [
        s
        for s in sources
        if s.get("role") == "original" and playable(s, packet["items"], packet["frozen_at"])
    ]
    if not originals:
        raise MissingMaterial("主推本体的近期发售、购买或可下载试玩条件未能核实")
    for source in originals:
        details = source.get("game_detail") or {}
        if "gallery" not in details and details.get("app_id"):
            fresh = materials.fetch_material(source["url"])
            media = fresh.get("game_detail") or {}
            if (
                fresh.get("status") == "read"
                and media.get("app_id") == details["app_id"]
                and source["content"] in fresh["content"]
            ):
                source.update(
                    {
                        k: fresh[k]
                        for k in (
                            "content",
                            "content_sha256",
                            "game_detail",
                            "images",
                            "images_version",
                            "fetched_at",
                        )
                    }
                )
                details = media
        if len({i["url"] for i in details.get("gallery", [])}) >= 2 and details.get("trailer"):
            trailer = details["trailer"]
            if not trailer.get("manifest_sha256"):
                try:
                    manifest = collect.fetch(trailer["stream"])
                except (OSError, ValueError) as exc:
                    raise MissingMaterial("官方预告播放清单未读到：" + str(exc)) from exc
                if not manifest.lstrip().startswith("#EXTM3U"):
                    raise MissingMaterial("官方预告未返回 HLS 播放清单")
                trailer["manifest_sha256"] = hashlib.sha256(manifest.encode()).hexdigest()
                trailer["verified_at"] = collect.now()
            proof = f"Steam 商店媒体栏：本作截图 {len(details['gallery'])} 张，官方预告《{trailer['title']}》。"
            if proof not in source["content"]:
                source["content"] += "\n" + proof
                source["content_sha256"] = hashlib.sha256(source["content"].encode()).hexdigest()
            return source
    raise MissingMaterial("主推需有两幅以上同一作品官方实机截图及官方演示播放器")


def material_for(db, app):
    url = f"https://store.steampowered.com/app/{app}/?cc=CN&l=schinese"
    saved = db.execute("SELECT payload FROM sources WHERE url=?", (url,)).fetchone()
    source = json.loads(saved[0]) if saved else {}
    if source and instant(source["fetched_at"]) + timedelta(days=30) > datetime.now().astimezone():
        content = source.get("content", "")
        if hashlib.sha256(content.encode()).hexdigest() == source["content_sha256"]:
            return source
    source = materials.fetch_material(url)
    if source.get("status") != "read":
        return None
    source = materials.bind_material(source, "reference", [])
    db.execute(
        "INSERT OR REPLACE INTO sources VALUES (?,?)", (url, json.dumps(source, ensure_ascii=False))
    )
    return source


def playable(original, items, cutoff):
    details = original.get("game_detail") or {}
    if details.get("app_id"):
        if not any(
            (item.get("game") or {}).get("app_id") == details["app_id"]
            and item["ref"] in original["origin_ref"]
            for item in items
        ):
            return False
        when = instant(stamp(details.get("date"), cutoff))
        if details.get("dlc") or not (details.get("demo") or details.get("offers")):
            return False
        # A demo on an old or upcoming page requires a separately dated new demo event.
        if when and 0 <= (instant(cutoff).date() - when.date()).days <= 7:
            return True
        return bool(
            details.get("demo")
            and any("试玩" in (x.get("summary") or "") and x.get("event_time") for x in items)
        )
    if details.get("mobile_actions"):
        return any("下载" in a for a in details["mobile_actions"]) and any(
            x.get("event_time")
            and instant(x["event_time"]) <= instant(cutoff)
            and any(
                word in (x.get("summary") or "") for word in ("上线", "开测", "测试", "试玩", "首发")
            )
            for x in items
        )
    return False


def attach(root, packet, checked, cancel, library):
    refs = sorted(x["ref"] for x in packet["items"])
    original = next(
        (
            s
            for s in checked
            if s.get("role") == "original"
            and set(refs).intersection(s.get("origin_ref", []))
            and playable(s, packet["items"], packet["frozen_at"])
        ),
        None,
    )
    if not original:
        raise MissingMaterial("缺少主推本体近期发售、购买或可下载试玩的对应证据")
    content = original["content"]
    matches = sorted(
        [
            (sum(word.casefold() in content.casefold() for word in words), app, subject)
            for app, subject, words in REFERENCES
            if app != (original.get("game_detail") or {}).get("app_id")
        ],
        reverse=True,
    )
    library.parent.mkdir(exist_ok=True, parents=True)
    with sqlite3.connect(library, timeout=30, isolation_level=None) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS angles (basis TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        candidates = []
        for score, app, subject in matches[:3]:
            if not score:
                break
            source = material_for(db, app)
            fields = (source or {}).get("game_detail") or {}
            if fields.get("app_id") == app and subject.casefold() in fields.get("name", "").casefold():
                candidates.append((fields["name"], source))
        if not candidates:
            raise MissingMaterial("资料库内没有命中该玩法的独立作品材料")
        # Match against the other work's gameplay only. Purchase offers, tags and prices are irrelevant.
        passages = [s["content"].split("标签：")[0][:4200] for _, s in candidates]
        basis = hashlib.sha256(
            json.dumps(
                [
                    "angle-v1",
                    packet["line"]["production_rules"]["research"],
                    original["content_sha256"],
                    [s["content_sha256"] for _, s in candidates],
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        old = db.execute("SELECT payload FROM angles WHERE basis=?", (basis,)).fetchone()
        if old:
            result = json.loads(old[0])
        else:
            schema = review.obj(
                {
                    "index": {"type": "integer", "minimum": -1, "maximum": len(candidates) - 1},
                    "angle": review.S,
                    "main_quote": review.S,
                    "background_quote": review.S,
                    "missing": review.S,
                }
            )
            prompt = (
                "选一个能帮助读者理解主推具体玩法的问题。只用给出的原文。逐字摘取两句，各自在所属作品的正文中。"
                "副文提供可比较的具体设计或玩家动作，不能只因题材相似而硬扯。找不到就 index=-1，填写 missing。\n"
                + json.dumps(
                    {
                        "主推": content[:6500],
                        "背景": [
                            {"index": i, "subject": sub, "content": p}
                            for i, ((sub, _), p) in enumerate(zip(candidates, passages))
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            result = review.call_model(root, "production-research", schema, prompt, cancel)
            if result["index"] >= 0:
                if (
                    not result["angle"].strip()
                    or not materials.verify_quote(content, result["main_quote"])
                    or not materials.verify_quote(passages[result["index"]], result["background_quote"])
                ):
                    raise MissingMaterial("背景匹配引文未能在所属作品原文中定位")
            db.execute(
                "INSERT OR REPLACE INTO angles VALUES (?,?)",
                (basis, json.dumps(result, ensure_ascii=False)),
            )
        if result["index"] < 0:
            raise MissingMaterial(result["missing"] or "候选背景不能解释主推玩法")
        subject, source = candidates[result["index"]]
        return dict(
            source,
            usage="background",
            subject=subject,
            applies_to_refs=refs,
            angle=result["angle"],
            background_quote=result["background_quote"],
            main_quote=result["main_quote"],
        )


def platform_coverage(pool_path, main, packet, cancel):
    """Read matching platform articles that had already entered the pool at edition time."""
    details = (main or {}).get("game_detail") or {}
    app = str(details.get("app_id") or "")
    if not app.isdigit() or not pool_path.is_file():
        return []
    with sqlite3.connect(pool_path) as db:
        rows = db.execute(
            "SELECT payload FROM collection_items WHERE observed_at BETWEEN ? AND ? "
            "AND (item_id LIKE 'game_xbox_wire:%' OR item_id LIKE 'game_ps_blog:%') "
            "ORDER BY observed_at DESC",
            ((instant(packet["frozen_at"]) - timedelta(days=21)).isoformat(), packet["frozen_at"]),
        ).fetchall()
    if not rows or cancel.is_set():
        return []

    def normalize(name):
        return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", name).casefold()))

    names = {normalize(details.get("name") or "")}
    localized = materials.fetch_material(f"https://store.steampowered.com/app/{app}/?cc=US&l=english")
    english = localized.get("game_detail") or {}
    if english.get("app_id") == app:
        names.add(normalize(english.get("name") or ""))
    names = {n for n in names if len(n) >= 4}
    hosts = {"game_xbox_wire": "news.xbox.com", "game_ps_blog": "blog.playstation.com"}
    seen, result = set(), []
    for (payload,) in rows:
        item = json.loads(payload)
        url = item.get("url") or ""
        if url in seen or cancel.is_set():
            continue
        seen.add(url)
        host = hosts.get(item.get("source_id"))
        if not host or urlsplit(url).hostname != host:
            continue
        # A publisher's headline must name the complete game. Reject a subtitle
        # or a sequel number appended to a shorter game's title.
        segments = re.split(r"\s+[–—-]\s+|\s*\|\s*", item["title"])
        titles = {normalize(item["title"]), normalize(segments[0])}
        matched = names & titles
        if not matched:
            continue
        story = materials.fetch_material(url)
        if story.get("status") != "read" or urlsplit(story.get("final_url") or url).hostname != host:
            continue
        if not any(" " + name + " " in " " + normalize(story["content"]) + " " for name in matched):
            continue
        source = materials.bind_material(story, "reference", main["origin_ref"])
        result.append(
            dict(source, usage="same_game_coverage", subject=details["name"], discovery_ref=item["ref"])
        )
        if len(result) == 2:
            break
    return result


def prepare(root, packet, checked, cancel):
    main = require_pick_gallery(checked, packet)
    existing = [
        s
        for s in checked
        if s.get("usage") == "background"
        and s.get("applies_to_refs") == sorted(x["ref"] for x in packet["items"])
    ]
    background = (
        existing[0]
        if existing
        else attach(root, packet, checked, cancel, Path(root).parents[2] / "game-background.sqlite3")
    )
    sources = [s for s in checked if s.get("usage") != "background"]
    if not any(s.get("usage") == "same_game_coverage" for s in sources):
        seen = {s["url"] for s in checked}
        coverage = platform_coverage(Path(root).parents[2] / "pool.sqlite3", main, packet, cancel)
        sources.extend(s for s in coverage if s["url"] not in seen)
    return {
        "materials": sources + [background],
        "unknowns": (packet.get("assessment") or {}).get("unknowns", []),
    }


def screen_event(root, event, items, line, cancel):
    sources = [review.material_content(root, m) for m in event["materials"] if m["status"] == "read"]
    packet = {"items": [items[r] for r in event["refs"]], "line": line, "frozen_at": cancel.frozen_at}
    try:
        main = require_pick_gallery(sources, packet)
        if main:
            event["materials"] = [
                review.persist_material(root, main) if m["material_id"] == main["material_id"] else m
                for m in event["materials"]
            ]
        background = attach(
            root,
            packet,
            sources,
            cancel,
            getattr(cancel, "background_library", Path(root).parents[1] / "game-background.sqlite3"),
        )
        event["pick_background"] = review.persist_material(root, background)
        event.pop("pick_gap", None)
    except MissingMaterial as exc:
        event.pop("pick_background", None)
        event["pick_gap"] = str(exc)

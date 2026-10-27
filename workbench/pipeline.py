"""One bounded collection pass; no article detail requests."""
from pathlib import Path
from . import collect, storage


def run(db, fetcher=collect.fetch):
    conn = storage.connect(db)
    batch, started = storage.begin_batch(conn)
    folder = Path(db).parent / (Path(db).stem + "-snapshots") / str(batch)
    folder.mkdir(parents=True, exist_ok=True)
    responses = {}
    results = []
    try:
        for source in collect.SOURCES:
            group = "tophub" if source in ("weibo", "zhihu") else source
            if group not in responses:
                try:
                    raw = fetcher(collect.URLS[group])
                    (folder / (group + (".html" if group in ("baidu", "tophub") else ".json"))).write_text(raw, encoding="utf-8")
                    responses[group] = (raw, None)
                except Exception as exc:
                    responses[group] = (None, f"{type(exc).__name__}: {exc}")
            raw, error = responses[group]
            try:
                if error: raise ValueError(error)
                rows = collect.PARSERS[source](raw)
                if not rows: raise ValueError("榜单为空或无法解析")
                storage.save_rows(conn, batch, rows)
                results.append({"source_id": source, "status": "success", "count": len(rows), "error": None})
            except Exception as exc:
                conn.rollback()
                results.append({"source_id": source, "status": "failed", "count": 0, "error": f"{type(exc).__name__}: {exc}"})
        finished = storage.end_batch(conn, batch, results)
        return {"batch": {"id": batch, "started_at": started, "finished_at": finished}, "source_results": results}
    finally:
        conn.close()

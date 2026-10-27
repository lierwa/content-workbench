# 本地热点工作台 API

运行：`python -m workbench serve --host 127.0.0.1 --port 8765`；单次采集：`python -m workbench collect`。需要 Python 3.9+，仅用标准库。可用 `--db PATH` 指定隔离 SQLite（位于子命令之后），默认 `data/workbench.sqlite3`。仅绑定回环地址，静态文件由 `workbench/static/` 同源提供；禁止跨站写入。数据均为 JSON，UTF-8；失败返回适当 HTTP 状态及 `{ "error": { "code": "...", "message": "..." } }`。状态更改只接受 `Content-Type: application/json` 和同源 Origin（无 Origin 的本机 CLI 请求允许）。

## 端点

- `GET /api/health` → `{ "ok": true }`
- `GET /api/config` → `{ "scheduler": { "enabled": false, "times": [] }, "timezone": "Asia/Shanghai" }`。时间配置目前未启用，服务不轮询、不注册系统任务。
- `POST /api/collect`，body `{}` → `{ "batch": {"id", "started_at", "finished_at"}, "source_results": [{"source_id", "status": "success"|"failed", "count", "error"}] }`。逐源隔离，返回数包括成功和失败的来源。并发采集返回 409。
- `GET /api/batches` → `{ "items": [{"id", "started_at", "finished_at", "source_results": [...]}] }`。
- `GET /api/candidates?selection=&source=&source_type=&q=&limit=&offset=` → `{ "items": [Candidate], "total": N }`；筛选空值视为全部，limit 默认 50 最大 200，offset 默认 0。
- `POST /api/candidates/selection`，`{"ids":["..."],"selection":"pending|selected|deferred|excluded"}` → `{"updated":N}`。一次性更改已存在的多个候选。
- `GET /api/candidates/{id}` → `{ "candidate": Candidate, "appearances": [...], "details": [...], "draft": Draft|null }`。
- `POST /api/candidates/{id}/details`，仅 selected，传 `{"source_url":"https://..."}` 抓取，或 `{"manual_text":"...","source_url":"https://..."}` 手工录入并可附来源链接（不会请求该链接）→ `{ "detail": Detail }`。无法取得有效正文时状态为 `blocked` 或 `needs_source` 并记录原因；手动文本为 `provenance=manual`。抓取 URL 不得指向本机或私有地址，有限时和大小，访问受限不绕过。更新材料会取消旧确认，需复核。
- `PUT /api/candidates/{id}/draft`，仅 selected，`{"title":"...","body":"..."}` → `{ "draft": Draft }`；每次保存生成新 revision，清除旧确认。
- `POST /api/candidates/{id}/confirm`，仅 selected，`{"revision":N}` → `{ "draft": Draft }`；仅确认当前 revision，缺少草稿或修订冲突返回 409，确认不是发布。将选中项改为其他状态保留草稿但取消确认。

Candidate：`{id,title,event_time,published_at,source_url,cover_url,source_id,source_name,source_type,collected_at,metrics,platform_snapshot_time,selection,possible_duplicates}`。未知值为 null；`metrics` 是来源指标原始键值，`possible_duplicates` 是其他候选 ID 列表，只提示不自动合并。`source_type` 当前为 `current_hotlist`；`historical` 仅预留，尚无历史采集。

历史扩展点：`workbench.providers.HistoricalProvider` 描述统一基本字段契约，`register_historical(provider)` 登记实现；本版本不自带历史 provider，登记也不会自行触发采集，配置默认关闭。未来采集实现需显式接入、保留历史事件时间和来源，不得把采集时间当事件时间。

Appearance：`{source_id,source_name,source_url,collected_at,metrics,platform_snapshot_time,batch_id}`。Detail：`{id,status,provenance,source_url,collected_at,text,reason}`，status 为 `obtained|blocked|needs_source`，provenance 为 `manual|web`。Draft：`{title,body,revision,confirmed_revision,updated_at}`；`confirmed_revision=null` 表示未确认。外链/封面只记录 URL，获取详情不会下载封面；采集榜单不获取详情。

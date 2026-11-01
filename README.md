# 内容工作台

## 栏目机制与界面交付（2026-11-01）

[工作台](http://127.0.0.1:18765/#/columns) · [交互原型](http://127.0.0.1:18765/prototype.html#/columns) · [架构与界面设计](docs/架构与界面设计.md) · [验收记录与截图](docs/架构与界面验收-20261101.md)

栏目由页面创建，分别选择渠道、筛选规则、创作要求和运行安排；作品汇总各平台稿及各自投递结果。配置保存在本地 data/column-config.json。前端构建同时产生实际入口和 prototype.html 原型入口。


目标：让同一套系统持续采集素材，由不同栏目线独立选稿、制作和发布，逐步实现自动化发帖。首批目标平台是微信公众号和今日头条。

**当前方案：持续采集 → 线索池 → 栏目线 → 筛选、取材 → 单篇或集合贴 → 审稿 → 发布及回读。**

## 从哪里读

| 要了解什么 | 唯一入口 |
| --- | --- |
| 项目目标、栏目线、最小范围 | [内容经营任务书](docs/内容经营任务书.md) |
| 数据、执行和开发顺序 | [开发实施说明](docs/开发实施说明.md) |
| 栏目选稿方式与现有三筛 | [栏目筛选约定](docs/三轮筛选规则与实现约定.md) |
| 单篇、集合贴如何制作和审稿 | [创作与审稿约定](docs/二创规则与独立审稿约定.md) |
| 来源适用于哪条线、实际查到了什么 | [来源清单](sources/来源清单.md) |
| 首个账号与栏目线的关系 | [账号卡](accounts/首个账号.md) |

2026-11-01 重整：以上文件是当前设计依据。日期交接、来源试采和 research 下的调查保留为历史材料；历史批次数量、账号状态及调用配置不构成当前要求。新会话先读 AGENTS.md 和内容经营任务书。

## 已有实现与目标设计分开

入口仍为 single_pass。采集写入持久线索池，按栏目独立筛选；当前接入 15 个采集渠道，科技栏目选择 6 家媒体。具体入口、正文可读边界及实采证据见 [来源清单](sources/来源清单.md)。[线索池](http://127.0.0.1:18765/#/pool) 提供分类、来源、栏目、时间和处理状态筛选；渠道配置在“渠道设置”。

已实现两表线索池、热点精选与科技简报两条栏目线、采集与筛选分离、集合编排及整篇审稿。定时配置默认关闭，真实平台验收结果单独记录；不能视为已具备稳定无人值守发布。

2026-11-01 已按会话授权开发并用 12 条全新内容验证，止于草稿。实际结果、首次失败与消耗见 [本轮验收记录](docs/栏目线实现验收-20261101.md)。

## 现有程序运行方式

下列命令启动栏目线工作台。运行安排在 single_pass/columns.json 配置；interval_minutes 为 null 表示关闭。平台操作仍按当次授权范围执行。

Windows PowerShell 首次准备：

```powershell
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
```

直接启动现有程序：

```powershell
.\.venv-win\Scripts\python.exe -m single_pass --port 18765 --data-dir data/single-pass-v2
```

已有 data/single-pass-v2 和前端构建时，可使用 start-workbench.cmd。该脚本检查项目归属并隐藏启动服务；缺少数据目录时会报错。入口为 http://127.0.0.1:18765/。

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python -m single_pass --port 18765 --data-dir data/single-pass-v2
```

筛选调用本机已登录的 Codex CLI。当前筛选配置读取 single_pass/screening_policy.json；不把旧文档中的固定预算、模型档位或重试次数当成配置。调用与 token 消耗以实际回执为准。

现有排版使用 vendor/doocs-md 的主题资源及 Python 解析、清洗组件；没有集成 doocs/md 的核心编辑器。平台结果以目标账号回读为准。

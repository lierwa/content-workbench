# 内容工作台

## 采集、批量选题与本地制作（2026-11-02）

- 采集操作统一在线索池，设置保存在数据目录中的 `collection-settings.json`；选题页不提供采集入口。
  默认全渠道，可按多个栏目取渠道并集、或自定义渠道；每渠道读取上限包含重复条目。
  点击“保存设置”仅保存；“立即采集”独立执行。计划支持手动、按分钟间隔、每天定时（北京时间），
  页面关闭不影响服务内调度，服务停止期间不会执行；服务停止期间错过的每日采集不补跑，间隔模式最多补一次。
  更换渠道或条数不重置计时，定时到点若已有采集则跳过该次，避免重叠。
  渠道启停仍在“渠道设置”，采集间隔统一在采集设置器管理。
- 选题页可多选或选择全部栏目，批量筛选最多同时执行两个栏目；各栏目结果、继续与停止相互独立。
  “更新选题”设置本轮栏目及时间区间或采集批次；范围内的增量线索全部纳入，不再设置选题输入条数上限或名单预览。
  同一栏目已处理且内容未变化的线索、制作中或已采用的线索不重复进入新一轮。
  开始时固定本轮输入；初筛、二筛分段保存结果，筛选过程侧栏支持查看各阶段与停止、续跑。
  续跑沿用原任务名单和已完成进度；新一轮结束旧任务并保留已完成结果。
  旧采集缺少逐条批次关联时不能按批次取数，新采集会保存批次关联。
  跨栏目勾选后加入制作队列，单篇逐条制作，集合按栏目分别组稿。
  相同选择重复提交复用原任务；手动选取本期其他未采用消息可另做一份集合稿，自动制作仍按栏目周期去重。
- 审稿报告展示当前版本是否通过、具体问题和修改建议。修改后保存，再检查当前稿件；独立审稿不改稿、不配图、不自动存入平台。
  编辑工作区参照 [Tiptap Simple Editor](https://tiptap.dev/docs/ui-components/templates/simple-editor) 的单行工具栏结构，保留 Vue / Tiptap 内核，支持材料对照、预览和保存状态。
- 制作默认生成**本地稿**，不要求平台账号。制作队列最多同时执行两篇，支持取消、失败重试，
  服务重启会恢复未完成制作。手动制作不自动同步到平台，公开发布仍需单独确认。
- 筛选与制作启动前检查 Codex CLI；会跳过缺少原生依赖的 npm 入口。
  Windows 检查 PATH 中的 `codex.exe` / `codex.cmd`，macOS 同时检查桌面应用内置 CLI。
  桌面环境提供的 `CODEX_APP_RUNTIME_CLI_PATH` 会优先作为自动探测候选。
  可通过 `CODEX_BINARY` 显式指定可执行文件；指定路径失效时明确报错，不静默替换。
  CLI 仍需在本机登录，网页可打开不代表模型和浏览器取材能力已经可用。
- 二筛引文校验失败时只修正出错条目一次，复用已完成的初筛；修正后仍须通过原文定位校验。
  同一事件某个来源读取失败时继续读取其他来源，未读来源不算已核实依据。

新接口：`GET /api/collection`、`POST /api/collection/settings`、`POST /api/collect`
（携带采集设置 `expected_revision`）、`GET /api/topics`、`GET /api/screen/options`、
`POST /api/screen/preview`、`POST /api/screen/start`、`GET /api/screen/process`、`POST /api/screen/batch`、
`POST /api/production/batch`。制作的 `targets: []` 表示本地稿。
原有按来源分别设置的采集间隔不再由调度器执行，统一采集计划初始关闭。

此前 macOS 验收记录（业务数据现已按要求清空）：全渠道即时与定时采集均执行成功，两个栏目产出 6 个可做稿选题，
无平台账号生成并通过审稿的本地稿共 3 篇；跨栏目批量提交、编辑保存和重启保留均已验证。
临时检查覆盖 10 栏目/10 稿件的并发上限、排队取消、重复提交和部分失败；这些队列检查替代了模型执行，
不作为真实内容质量验收。Windows 的 CLI 路径分支经过模拟检查，尚无 Windows 实机结果。
该次验收的浏览器扩展为 0.4.0 / 协议 1.7，与 CLI 0.4.1 默认目标控制要求不匹配，
百度入口的一条选题仍待取材；其他已核实来源可正常做稿。参见 [BrowserSkill 升级说明](https://github.com/Tencent/BrowserSkill/blob/main/CHANGELOG.md#041---2026-11-02)。
取材任务会话已关闭；Chrome 中一个已关闭的 BrowserSkill 保存分组归属未能确认，未删除。

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

# 单次热点采集与筛选

手动采集一次头条、百度、微博和知乎榜单，再对当前批次执行筛选。旧批次中的 B 站条目不参与筛选；原始采集及初筛记录保留。

## 运行

Windows PowerShell 原生运行（无需 WSL）：

```powershell
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
.\.venv-win\Scripts\python.exe -m single_pass
```

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python -m single_pass
```

打开 `http://127.0.0.1:18765/`。服务只监听本机；若端口已被占用，先确认占用者。Python 依赖见 `requirements.txt`。

本次 Windows 运行中，新版工作台使用 `http://127.0.0.1:18766/` 和独立数据目录 `data/single-pass-v2/`，原 18765 服务及其数据保留。如需在原服务仍运行时手动启动这个新版实例，可使用：

```powershell
.\.venv-win\Scripts\python.exe -m single_pass --port 18766 --data-dir data/single-pass-v2
```

筛选复用本机已安装并登录的 Codex CLI：优先读取 `CODEX_BINARY`，其次查找 `PATH` 中的 `codex`（Windows 优先 `codex.exe`），最后兼容 macOS 应用内置路径。启动前在同一终端确认 CLI 版本和登录状态；版本须支持 `gpt-6.1-terra` 和 `ultra`。如需明确指定 Windows 可执行文件，可在启动前设置 `$env:CODEX_BINARY = 'C:\path\to\codex.exe'`。文件锁在 Windows 使用 `msvcrt`，macOS / Linux 使用 `fcntl`。

## 使用

1. 点击采集，等待四个来源返回；失败时显示原因。微博和知乎通过第三方 TopHub 获取，采集时间与来源提供的报道时间分别记录。
2. 点击筛选：先对本批条目按[八项规则](docs/筛选规则.md)初筛，再一次归并事件并确定最多 12 个优先取材事件。其余事件保留未进入取材的具体原因，不视作已读或淘汰。取材和复筛每批最多四个事件，最多两个模型调用同时进行；最后比较有证据的事件。只能依据真实取得的正文及对应原句判断；无法取得证据时保留为待核。最多展示八个优先候选，不保证凑满。旧批次保留原计划，不套用新的 12 事件上限。
3. 页面可查看原初筛理由、事件关联、材料、原句、复筛与最终取舍。用户可以停止；再次手动继续时复用已完成的分段和事件，补做未完成部分。不会自动重采或自动无限重试。
4. 最终候选自动生成推荐排名，页面直接展示名次、具体看点、读者参与点、为什么排在下一名之前及待核信息。“为什么选出这些候选”展示本批取舍过程；“查看依据”可核对正文原句。旧批次点击“生成详细排名”，也可手动重新排名；只比较现有候选，保留原入选决定，按来源筛选不改变全批名次。排名与调用记录随批次保存，不预测流量，也不按标签数量或跨平台热度相加。
5. 在生产区添加平台账号并连接：默认使用已连接扩展的本机浏览器中的专用窗口；也可选择为账号建立独立浏览器资料，由用户登录。选择最终候选和目标账号后可批量创建稿件，通过可视化富文本编辑标题、摘要、正文与配图，调整封面，并按公众号或头条的排版预览。生产状态独立更新，每篇稿件的结果分别记录。
6. 审阅后按页面显示的实际平台能力同步、核验和确认发布。发布需要对具体稿件明确确认；页面显示不可用的能力不能当作已完成。两平台的当前浏览器模式草稿保存、公众号独立资料模式草稿保存及部分草稿回读已有代码，但尚无真实平台验收；公众号发布和结果核验仍有缺口。实际发布及结果核验均未完成真实验证，不能将完整流程视为已验收。

数据保存在 `data/single-pass/`，包括原始榜单快照、初筛结果、模型原始事件、取材正文、复筛和比较记录。模型固定为本机 Codex CLI 的 `gpt-6.1-terra` 和 `ultra`；筛选会产生真实模型和联网调用。页面显示实际已记录的 token 用量，不估算费用。

同一批次相同规范 URL 的已读正文会复用，事件判断仍分别进行。阶段耗时中的模型累计耗时是各次调用耗时之和，不等于任务总历时；旧记录缺少精确计时的部分按文件时间估算并标注，缺少用量的调用不会伪装成完整记录。新的批量规模尚未经真实筛选验证。

本地创作在重启后继续处理未完成任务；平台操作中断时标记为结果未知，需核验后再处理，不自动重复提交。账号连接中断后需重新连接。使用当前浏览器需要本机 `bsk` 服务及已连接的 Chrome 扩展；独立资料模式使用本机 Chrome 或 Edge，不下载浏览器。平台账号资料和封面保存在 `data/single-pass/`，请妥善保护该目录。

公众号排版复用 [doocs/md](https://github.com/doocs/md) 的 default 和 simple 主题 CSS，来源版本和许可证见 `vendor/doocs-md/`；内容处理使用 markdown-it-py、nh3 和 premailer，本项目没有接入 doocs/md 的核心编辑器。头条使用单独的简洁样式。排版依赖由 `requirements.txt` 引入，实际平台编辑器的呈现和保存仍需实机核验。

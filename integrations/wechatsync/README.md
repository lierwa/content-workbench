# Wechatsync 接入

平台账号检查、草稿提交、图片上传复用官方 Wechatsync 2.0.9 扩展。工作台保留任务、审稿、版本、去重和回读校验；不再维护两套平台编辑器点击和粘贴流程。正式发表能力关闭，草稿须到平台确认发表。

## 安装

使用 Node.js 24（本轮为 24.14.1），在仓库根目录执行：

```powershell
npm.cmd --prefix integrations/wechatsync ci --ignore-scripts --no-audit --no-fund
.\.venv-win\Scripts\python.exe integrations/wechatsync/prepare.py data/single-pass-v2
```

在现有 Chrome 的扩展管理页加载命令返回的目录。更新接入代码后重新运行准备命令，并重新加载同一个扩展，保留 Chrome 用户资料及平台登录。Windows 完整流程仍由 `./start-workbench.ps1 -CheckRuntime` 启动。同步时服务自动启动本地桥接进程；连接只监听 `127.0.0.1:19527/19528`，写请求必须带随机本地密钥。

运行文件、密钥和操作记录全部位于已忽略的 `data/single-pass-v2/platform-accounts`，不提交远程仓库。复用工作台已有任务窗口，不借用用户窗口、不按文章创建新会话。

## 上游与修改范围

- [官方源码](https://github.com/wechatsync/Wechatsync/tree/42f61e39e214b8d5377d09d788a716defc4abdc0)，GPL-3.0；许可证在 `upstream/LICENSE`。
- [官方发布包 2.0.9](https://wpics.oss-cn-shanghai.aliyuncs.com/wechatsync-2.0.9.zip?date=20260324)，SHA-256：`dfb191decd63a45063c4f6d8f7687745e5ecebe34363792dd6781b657d69fd29`。公开源码与发布包包含的适配器并不完全相同：本接入用发布包中的公众号和头条适配器，准备脚本验证固定校验和，不追随未知更新。
- `upstream/ws-bridge.ts` 源自上述提交的 MCP bridge；本地修改为环回地址监听、扩展 Origin 检查、HTTP 请求鉴权和取消通配 CORS。Node 需支持 `--experimental-transform-types`。
- `workbench.js` 补齐摘要、封面、已有草稿 ID、图片样式保留、指定任务标签、完整回读和账号一致性。公众号更新同时设置 `sub=update` 和 `AppMsgId`；只传 ID 仍会新建。头条草稿按现行官方页面使用 `save=0`、无广告 `article_ad_type=2`、单图封面 `coverType=2`。头条上传与其上游提交方法一样在已登录页面执行，以使用正常页面请求环境；不处理验证码或绕过登录。
- Chrome MV3 会挂起闲置服务线程。本地 WebSocket 每 20 秒交换一次心跳，**不请求公众号或头条**；依据 [Chrome 官方说明](https://developer.chrome.com/docs/extensions/how-to/web-platform/websockets)。断线通过 Chrome alarm 恢复。

## 登录与续期

登录保存在现有 Chrome 用户资料中，关闭工作台不会删除。头条每 6 小时核验一次已经登记的账号；这不是强制延长平台凭证期限。公众号采用其官方 `switchacct?action=switch` 接口尝试同账号续期；成功后每 6 小时运行一次，任何时候都核对同一账号身份。

2026-11-03 本机公众号登录有效，但续期返回 `201501`（未授权账号切换）。该状态不能当作续期成功；需重新扫码授权后再核验。切换到当前账号的网页按钮直接返回，不会发出续期请求。WeRSS 的多账号轮换也不能证明单账号已经续期成功。

## 提交与恢复

创建前核对完整草稿列表；同名稿不会被盲目覆盖或重复创建。提交前保存操作 ID、账号、版本及内容哈希。超时或未知结果禁止重新创建；恢复只回读原操作已记录的草稿 ID。原位更新先核对平台稿与上次回读一致，防止覆盖用户在平台的修改。回读核对标题、摘要、正文、图片位置、样式、封面及账号；平台换图地址时比较实际图片内容。

旧编辑器操作的未完成检查点保留为历史，不自动改造成新稿；没有新接入的操作记录时明确要求核对原草稿。

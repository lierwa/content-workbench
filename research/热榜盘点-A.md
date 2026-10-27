# 热榜实际可访问性盘点 A（2026-10-28）

本文件链接的 `runs/` 原始响应仅保存在原电脑，不随仓库提交；以下公开入口和采样摘要仍可查看。

本地采样时间均为北京时间（UTC+8）。本次为无登录、无新增依赖的标准库公开请求；请求样本只证明当时环境一次可访问，不保证无人值守长期稳定，也未验证榜单事件正文、历史与发布时间。平台字段不得跨平台混算。

| 平台及官方入口 | 本次结果 | 数据/覆盖与边界 |
| --- | --- | --- |
| [今日头条热榜公开接口](https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc) | 00:39:28，HTTP 200、`status=success`，`data` 50 条；匿名直接 JSON 可读，不需要网页渲染。 | `data` 数组顺序可取，原始 `HotValue` 是平台热度字段，计算口径未知，不能称阅读数；榜单更新时间未独立验证。项目已有脚本此前成功重跑，本次接口单次复查成功。 |
| [微博热搜官方接口](https://weibo.com/ajax/side/hotSearch) | 00:39:28，HTTP 403，未获得榜单。一次替代 [微博热搜网页](https://s.weibo.com/top/summary?cate=realtimehot) 于00:41:26 返回 HTTP 200，但内容为 Sina Visitor System 访客页，未返回榜单。 | 目前无法通过此次无登录标准库请求读到条目、顺序或热度；不能把访客页当成功。未进一步处理身份验证，不代表所有网络环境或登录状态不可用。 |
| [知乎热榜接口](https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=10&desktop=true) | 00:39:28，HTTP 401：`AuthenticationError`，`身份未经过验证`。一次替代 [知乎热榜页](https://www.zhihu.com/hot) 00:41:26 返回 HTTP 403 和 `zse-ck` 脚本页面。 | 两种匿名请求均未取得榜单、题目或热度。未绕过身份验证或访问控制；本次结果不能推断知乎对所有访问场景一律要求登录。 |
| [百度热搜](https://top.baidu.com/board?tab=realtime) | 00:41:26，HTTP 200，匿名 HTML 可读，标准库解析响应中 `<!--s-data:...-->` JSON 成功，无需渲染；`data.cards[0].content` 共 51 项，含一项 `isTop=true` 置顶及 50 项其他内容。 | `word` 为题目，`index` 是原始字段，置顶与首条都为0，不能不加区分直接当排名；`hotScore` 是热搜热度字段，具体算法未核实，不是阅读数。仅本次匿名请求及解析成功，未验证重复稳定性。 |
| [哔哩哔哩全站排行榜公开接口](https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all) | 00:41:26，HTTP 200、`code=0`，`data.list` 100 项；匿名 JSON 可读，无需页面渲染。 | 条目有视频 `bvid`、`title`、`stat.view`、`stat.like`、`stat.reply` 等；`view` 是播放数，`like` 是点赞数，二者不等于热度或微信公众号阅读数。`his_rank` 为历史字段，不能当现排名；这里仅描述数组顺序，当前界面名次未核实。未验证重复稳定性。 |

## 实际样本

以下数字均为采样时响应字段，不是对题目事实的独立核实。头条 `data` 按返回顺序：

1. 中国足协与皇马俱乐部严正交涉：`HotValue=123784729`。
2. 17岁AI研究员被曝获700万年薪邀约：`HotValue=112005054`。
3. 看作风建设如何落到群众身边：`HotValue=101346364`。

百度 `content` 按返回顺序：

1. 置顶：习近平致电祝贺卢拉当选连任巴西总统，`isTop=true`、`index=0`、`hotScore=7904132`。
2. 外交部回应“姆巴佩遭中国公司解约”，`index=0`、`hotScore=7809492`。
3. 中国足协第一时间与皇马严正交涉，`index=1`、`hotScore=7712181`。

哔哩哔哩 `data.list` 按返回顺序：

1. [写了全国所有省份的love me！](https://www.bilibili.com/video/BV1w2Hw6KE1j/)：`stat.view=5482725`，`stat.like=580277`。
2. [旗舰手机芯片大横评：苹果高通发哥玄戒谁最强？](https://www.bilibili.com/video/BV1kMxM6FECM/)：`stat.view=1100316`，`stat.like=147156`。
3. [马尔芙小姐，我能请你共舞一曲吗？｜PV](https://www.bilibili.com/video/BV1kZHK6VEgZ/)：`stat.view=3539202`，`stat.like=219278`。

微博、知乎本轮没有获取可核验条目，不能给出相应的样本与数值。未查询任何第三方聚合，也未对外发布或创建账号。公开访问不证明内容可转载或可持续抓取；还需要核验原始材料与使用权限。

## 本地证据与项目状态

取样的响应摘要和页面分别见 [证据目录](../runs/hotlist-audit/2026-10-28/a/)；`baidu-parsed.json` 是从所存百度页面抽取的数量及前4项，`bilibili.json` 与 `toutiao.json` 含前三项及响应状态，`weibo.json`、`zhihu.json` 和两条网页尝试保留状态/响应页。知乎页面和微博访客页面仅作失败依据，不执行其中脚本。未修改现有采集脚本。

工作开始时 Git 为 main、无 commit，所有既有项目文件未跟踪；无创建 worktree、安装依赖、登录、提交、推送或发布。

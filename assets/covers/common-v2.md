# 公共底图 v2

日期：2026-11-01。

当前设计文件：[common-v2.png](common-v2.png)。此图不含文字、账号名、栏目名或日期，中央米白区域用于叠加文章标题。v1 是被用户纠正的旧设计，不应作为默认素材。

使用约定：各篇文章复用同一底图，由固定程序排入实际文章标题，不能将账号名当作标题，不能直接把空白底图当成最终封面上传。本文记录设计，标题排版和生产接入尚未完成。

选图顺序：原帖合适图片 → 同一事件相关报道的合适图片 → 以本底图排版文章标题形成文字封面。文字封面不自动当作新闻配图插入正文。

制作方式：内置 image_gen 编辑工具，基于 v1 移除全部文字；输出已目视检查，中央文字区为空。

最终提示词：

```text
Edit this reusable article cover background. REMOVE the three large Chinese characters “薄闻录” completely from the central warm off-white rectangle, restoring a completely clean blank off-white surface. Keep the entire existing design otherwise exactly the same: canvas dimensions and aspect ratio, turquoise/navy/coral palette, cream central rectangle, offset turquoise panel, diagonal corner shapes, white partial rings, and all their positions and sizes. The central rectangle must be empty so actual article titles can later be typeset there by code. There must be NO text, NO letters, NO numbers, NO logo, NO placeholder text or outlines anywhere. This is the reusable background asset, not a finished cover for a specific article. Do not add new graphic elements. Output the edited image only.
```

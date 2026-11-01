# 公共资讯底图 v6

日期：2026-11-01。文件：[common-v6.png](common-v6.png)。

## 已确认的方向

用户明确选择「保留辅助文字，做完整的资讯版式」。公共底图允许低对比的辅助字样参与构图；不包含账号名，文章标题是唯一醒目的主文字。不要再把「不放账号名」扩大成「背景完全无字」。

本图保留「热点 / 新知 / 观察」「NEWS」「观察」「NEWS · INSIGHT」等辅助排版。白色主标题区留空并保持水平，供固定程序绘制具体文章标题。它是所有文章复用的背景，不是某一新闻的成品封面。

## 状态

这是按已确认方向制作的新设计候选，不等于用户已经批准这一具体图稿。仅保存资产；尚未接入生产，标题排版和双平台裁剪、上传、回读未验收。不能直接上传空标题底图。

## 制作记录

内置 image_gen：第一步使用 36 氪早报图作为视觉语言参考，原创辅助文字和模块的组合；第二步只校正标题区，使其水平。已经目视检查图中文字、空白标题区和无账号名要求。

### 生成提示词

```text
设计一张原创中文资讯媒体的公共封面底图，横向约 2.2:1。参考图是视觉语言参考：学习它将文字、图形、透视、疏密节奏组织成完整编辑版式的方法，重新设计构图，不复制品牌或原有文字。

这次的关键是“辅助文字也是图形”，不能做成空白盒子或只有几条线的背景。采用饱和群青蓝、深蓝、亮蓝与白色，做精细的二维轴测平面设计。背景由相互咬合、错位延伸的薄平面形成，不画现实中的箱体。统一斜向轴线，正面的文字阅读面保持水平。

视觉层级：
1. 中央略偏左保留一条水平白色标题带，宽约画面75%、高约28%，可容纳后续程序叠加的两行文章标题。标题带完整干净，不放任何占位字。
2. 大幅、低对比的“NEWS”和“观察”作为背景字形，沿下方及侧面透视平面排布，可局部出血裁切，让字形本身成为构图骨架。它们是蓝底上的深蓝字，明显退后，不是第二个主标题。
3. 顶部用小字“热点 / 新知 / 观察”形成精确的横向编辑栏；底部用小号“NEWS · INSIGHT”及少量对齐短线建立细密节奏。小字、图形切口、标题带边缘都服从同一套对齐关系。局部辅以小范围的重复线格，带来资讯密度。

把三种尺度的文字和几何平面设计成一个整体：有紧有松，中心安静、外围有信息，平面之间自然衔接。强调字体的比例、字距、基线、粗细和图形化处理，不是给空白形状贴几个标签。清晰利落、有成熟媒体的识别感，细节丰富但阅读顺序明确。

只允许上述辅助字样。没有账号名、品牌LOGO、日期、固定时间、文章标题。不要实物渲染、厚重箱体、PPT边框、装饰圆环、柔光阴影。只输出一张完整公共底图。
```

### 标题区修正提示词

```text
Make ONE precise layout correction to this public article-cover background. Change the empty white headline area into a perfectly horizontal, straight rectangular strip: its top and bottom edges must be level with the image canvas, its left and right edges vertical. Keep it in the upper-middle region, around x=8% to x=82%, y=24% to y=49%, with enough space for two lines of headline text. It is a flat frontal reading surface; it must not follow the surrounding diagonal perspective. Keep the white area empty, without any text or labels. Preserve the rest of the image's overall composition, blue palette, background NEWS and 观察 typography, small 热点 / 新知 / 观察 labels, repeated patterns and lower NEWS · INSIGHT text. Only adjust adjacent graphic boundaries where necessary to accommodate the straight white title strip. Preserve the auxiliary lettering exactly. No new words or logos.
```

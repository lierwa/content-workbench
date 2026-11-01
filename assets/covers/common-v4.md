# 公共底图设计 v4

日期：2026-11-01。状态：待用户评审的设计稿，尚未接入自动生产。

- 公共底图：`common-v4.png`，不包含账号名或文章文字。
- 标题效果预览：`common-v4-preview.png`，用现有登月稿标题展示排版方向，不作为所有文章共用的封面。
- 正式使用时固定复用底图，程序独立绘制文章标题；不为每一篇调用图像生成模型。
- 这两张均由内置 image_gen 工具生成，已检查文字和空白背景。标题排版程序、不同标题长度、两平台裁剪及回读尚未验收。
- v1/v2 和未入项目的 v3 已被用户否定；不得把旧设计或本设计稿当作用户批准的生产默认。

参考：36 氪早报 https://eu.36kr.com/en/p/3710693707166083 。参考信息层级和图形深度，采用原创的蓝底白字与底部折带构图。

## 标题效果提示词

```text
Create a beautifully art-directed, original Chinese digital news cover. Use the supplied 36Kr image solely to understand its strong editorial typography, controlled monochromatic color, integrated spatial composition, and crisp youthful media identity. Make an ORIGINAL alternative, not a replica. This is a typography-led editorial graphic, NOT a PowerPoint slide, NOT a sports banner.

Output a single finished landscape image, approximately 2.2:1. The most important element is magnificent, huge, highly legible Chinese headline typography. Typeset the following exact two lines in bold, carefully spaced, upright modern Chinese sans serif:
“中国载人登月为何发射两次？”
“揽月将先无人彩排”
No other words, logos, dates, numbers, badges or brand names.

Composition: electric saturated indigo-blue full-bleed background, white headline set DIRECTLY ON THE BLUE, without any white rectangle or boxed text panel. Two headline lines form a disciplined left-aligned central block with roughly 9 percent left margin and generous top and bottom breathing space. First line smaller only as necessary to fit, second line larger and weightier, forming a decisive typographic hierarchy. Perfectly rendered simplified Chinese, accurate punctuation. This text is the primary visual and must carry the entire cover even at thumbnail size.

Build a refined but energetic ORIGINAL editorial layout underneath: one restrained continuous folded blue ribbon runs along the lower edge, turning up subtly at the far right to suggest a stream of information, not a frame. Its flat front and side faces are two closely related blue shades. Very subtle oversized parallel typographic baseline rules, aligned with the headline, appear only in the outer lower margin. A very small solid warm-white rectangular notch at the bottom edge is the only high-contrast accent outside the headline. The large quiet center remains a uniform vivid blue. Keep graphic detail sparse, controlled, architecturally precise, and subordinate to the type.

The result should look like a distinctive contemporary Chinese technology publication, with the sophistication and immediate recognition of a professional editorial design system. No floating white title board, no picture frame, no paper sheet mockup, no diagonal speed stripes, no corner brackets, no circles, no checkerboards, no arbitrary triangles, no gradients, no shadows, no photography, no illustrations or meaningless microtext. Do not include the reference brand, its slogan or its layout. This is a headline placement demonstration of a shared background; the background must remain attractive and reusable when the headline is removed.
```

## 公共底图提示词

```text
Create the reusable public background asset from this design by removing ALL white Chinese headline text and its punctuation. Restore the exact uniform vivid blue background underneath the letters. Preserve the rest of the design exactly: original image dimensions, every blue shade, all the lower folded ribbon geometry, the two thin lower-edge rules, and the tiny white horizontal rectangular accent at the lower right. Do not alter the geometry, colors, framing or positions. Do not add text, logos, characters, numbers, symbols or placeholders. The finished reusable background must have a clean, empty blue area covering the upper and central portion, reserved for headlines typeset later by a fixed program. Output only the background image.
```

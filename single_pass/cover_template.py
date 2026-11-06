"""Typeset the four editable captions onto a text-free cover plate for delivery."""
import os
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Coordinates are in the original 1860 x 845 cover artwork.
_BOXES = {
    'brand': ((1190, 30, 1530, 112), 34),
    'topics': ((345, 178, 925, 272), 51),
    'headline': ((221, 313, 1639, 532), 142),
    'footer': ((832, 576, 1380, 668), 42),
}


@lru_cache(maxsize=2)
def _font_path(bold):
    windows = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    candidates = [windows / ('msyhbd.ttc' if bold else 'msyh.ttc'),
                  Path('/System/Library/Fonts') / ('STHeiti Medium.ttc' if bold else 'STHeiti Light.ttc'),
                  Path('/usr/share/fonts/opentype/noto') / ('NotoSansCJK-Bold.ttc' if bold else 'NotoSansCJK-Regular.ttc')]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise ValueError('生成栏目封面需要微软雅黑、黑体或 Noto Sans CJK 字体')


def render(plate, labels, colors):
    if not isinstance(labels, dict) or set(labels) != set(_BOXES):
        raise ValueError('封面需要账号名、栏目、主文字和底部文字')
    if any(not isinstance(s,str) or len(s)>100 or len(s.splitlines())>1 for s in labels.values()):
        raise ValueError('每处封面文字须为 100 字以内的单行文本')
    with Image.open(plate) as source:
        canvas = source.convert('RGB')
    if canvas.size != (1860,845):
        raise ValueError('封面底板尺寸必须为 1860 × 845')
    draw = ImageDraw.Draw(canvas)
    for field, (box, size) in _BOXES.items():
        value = labels[field].strip()
        if not value:
            continue
        x1,y1,x2,y2 = box
        font_path = _font_path(field in ('headline','topics','footer'))
        while size >= 12:
            font = ImageFont.truetype(font_path,size)
            left,top,right,bottom = draw.textbbox((0,0),value,font=font)
            if right-left <= x2-x1 and bottom-top <= y2-y1:
                break
            size -= 1
        else:
            raise ValueError('封面文字过长，请缩短后保存')
        x = (x1+x2-left-right)/2
        y = (y1+y2-top-bottom)/2
        draw.text((x,y),value,font=font,fill=colors[field])
    out = BytesIO()
    canvas.save(out,format='PNG')
    return out.getvalue()

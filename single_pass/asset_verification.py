"""Compare platform-transcoded photos with the supplied local image."""
import hashlib
from io import BytesIO
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from PIL import Image, ImageChops, ImageOps, ImageStat, ImageDraw


def verify_photo(url, local_bytes, platform):
    host = (urlsplit(str(url)).hostname or '').lower()
    allowed = host == 'mmbiz.qpic.cn' if platform == 'wechat' else host.endswith(('.toutiao.com','.byteimg.com','.toutiaoimg.com'))
    if urlsplit(str(url)).scheme != 'https' or not allowed:
        raise ValueError('平台图片来源无效')
    with urlopen(Request(url,headers={'Referer':'https://mp.weixin.qq.com/' if platform=='wechat' else 'https://mp.toutiao.com/'}),timeout=20) as response:
        final_host = (urlsplit(response.url).hostname or '').lower()
        if final_host != host: raise ValueError('平台图片跳转到其他来源')
        remote = response.read(20_000_001)
    if len(remote)>20_000_000: raise ValueError('平台图片过大')
    return compare_photo_bytes(remote,local_bytes)


def compare_photo_bytes(remote,local_bytes,excluded_rectangles=()):
    with Image.open(BytesIO(local_bytes)) as a, Image.open(BytesIO(remote)) as b:
        if max(a.width*a.height,b.width*b.height)>24_000_000:raise ValueError('图片尺寸过大')
        size=b.size
        # A browser screenshot can include the editor's persistent image toolbar.
        # Exclude only its measured rectangle from both images; never change
        # the page or silently loosen similarity thresholds.
        mask=Image.new('L',size,0)
        for rect in excluded_rectangles:
            if len(rect)!=4 or not (0<=rect[0]<rect[2]<=size[0] and 0<=rect[1]<rect[3]<=size[1]):
                raise ValueError('编辑器遮挡区域无效')
            ImageDraw.Draw(mask).rectangle(tuple(rect),fill=255)
        coverage=1-ImageStat.Stat(mask).mean[0]/255
        if coverage<0.95:raise ValueError('图片被编辑器遮挡超过 5%，无法完整核验主体')
        def visible(photo):
            photo=photo.copy();photo.paste((128,128,128),(0,0),mask)
            return photo.resize((128,128),Image.Resampling.LANCZOS)
        right=visible(b.convert('RGB'))
        def dhash(photo):
            data=list(photo.convert('L').resize((9,8),Image.Resampling.LANCZOS).getdata())
            return [data[y*9+x]>data[y*9+x+1] for y in range(8) for x in range(8)]
        matches=[]
        def compare_alignment(alignment):
            reference=ImageOps.fit(a.convert('RGB'),size,method=Image.Resampling.LANCZOS,centering=(alignment,alignment))
            left=visible(reference)
            error=sum(ImageStat.Stat(ImageChops.difference(left,right)).mean)/3
            distance=sum(x!=y for x,y in zip(dhash(left),dhash(right)))
            matches.append((error,distance,alignment))
        # Locate the crop, then refine within the best one-percent interval.
        # Text-heavy images can differ by several hash bits from a two-pixel
        # offset even when they are the same platform-transcoded picture.
        for step in range(101):
            compare_alignment(step / 100)
        center=round(min(matches)[2]*1000)
        for step in range(max(0,center-10),min(1000,center+10)+1):
            if step % 10:
                compare_alignment(step / 1000)
        accepted=[match for match in matches if match[0]<=6 and match[1]<=4]
        error,distance,alignment=min(accepted or matches)
    if error>6 or distance>4:
        raise ValueError(f'平台图片与本地素材未通过内容核对（颜色误差 {error:.2f}，结构差异 {distance}）')
    return {'comparison':'bounded_crop_color_and_structure','crop_alignment':alignment,'mean_color_error':round(error,3),'dhash_distance':distance,
            'visible_fraction':round(coverage,6),'editor_control_rectangles':[list(x) for x in excluded_rectangles],
            'local_sha256':hashlib.sha256(local_bytes).hexdigest(),'remote_sha256':hashlib.sha256(remote).hexdigest(),
            'remote_size':list(size)}

# 从照片 / PPT / 海报里裁切并解码二维码

展板、宣讲 PPT、签到表的屏摄里，二维码常常就是**唯一的投递入口**（还可能把内推码、渠道码嵌在 `?referralCode=` 参数里）。本文件是「把照片里的码变成可扫单码图 + 可点链接」的配方。

## 何时用

- 用户发来展板/PPT/海报照片，里面有一个或多个二维码
- 需要把码存下来供长期扫（单码图），或需要知道码背后的真实链接（投递入口、内推码、公众号、问卷）

## 配方

**0. 全部 cv2 / PIL 读写都在纯 ASCII 临时路径做**（原因见「坑」第 1 条）：

```bash
cp "/c/Users/<user>/AppData/Local/hermes/cache/images/img_xxx.jpg" "$LOCALAPPDATA/Temp/src.jpg"
cd "$LOCALAPPDATA/Temp"
```

**1. 先用 `detect()` 拿精确四角，别按比例估**（比例估算会切掉定位环）：

```python
import cv2, numpy as np
img = cv2.imread('src.jpg'); print(img.shape)
det = cv2.QRCodeDetector()
ok, pts = det.detect(img)
for i, q in enumerate(np.array(pts).reshape(-1, 4, 2)):
    print(i, q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max())
```

**2. 裁切 + 3–4× LANCZOS 放大，并把「能否解码」当完整性校验：**

```python
from PIL import Image
im = Image.open('src.jpg')
for pad in (0, 0.01, -0.01, 0.02):           # detect 拿不到角时：多试几组留白
    a, b, c, d = int((x1-pad)*W), int((y1-pad)*H), int((x2+pad)*W), int((y2+pad)*H)
    cr = im.crop((a, b, c, d)); cr = cr.resize((cr.width*4, cr.height*4), Image.LANCZOS)
    cr.save('code.png')
    print(pad, repr(cv2.QRCodeDetector().detectAndDecode(cv2.imread('code.png'))[0]))
```

留白取码宽的 5–8%；**解码成功的那组留白就是对的**（能解＝四角与留白都够）。

**3. 保存前用 `vision_analyze` 看一眼成品**：三个定位环是否都在、四边留白是否均匀、有没有切掉边缘或混入标题文字。像素级对齐靠人眼一眼最准，程序只会告诉你「解不出来」。

> **解不出的码（小程序码等）这一步是强制的，不是可选**：`detectAndDecode` 给不出结果时，你就同时失去了「能解码＝裁对了」这个判据——那时**唯一的口径就是肉眼**。凡是交付前没复核过的码，会被用户原样打回（「裁得不好」），而且他是在打开扫码时才发现。交付前看一眼，比事后重裁便宜得多。

**4. 存两份**：`…-投递二维码-<用途>.png`（单码，供长期扫）+ 原始屏摄（留证）。落地路径沿用情报归档目录，用 bash `cp` 从 ASCII 临时名复制到中文目标名（bash 处理中文路径没问题，cv2 才不行）。

## 坑

- **OpenCV 的 `imread` / `imwrite` 在含中文（非 ASCII）路径上读不到图**：返回 `None`，紧跟的 `detectAndDecode` 抛 `Assertion failed: !img.empty()`。**不是图坏了**——把文件复制到 ASCII 路径再读即可。（`cv2.imread` 在 Windows 上不走宽字符 API，这是库的行为，不是环境缺件。）
- **按比例估算裁剪不可靠**：切掉一个定位环，码就整张废掉，用户会直接反馈「裁得不好 / 扫不出来」。以 `detect()` 的四角或「解码成功」为准，并用 vision 复核。
- **小程序码（圆环定位点，微信 / 小红书那类）不是标准 QR，`QRCodeDetector` 解不出**：不要反复换参数空转，裁好直接交给用户手机扫，并在交付里说明「此格式程序解不出」。
- **同一活动多张图里重复出现的码，解码结果应当一致**（例：公众号码在两页 PPT 上都出现）——可当交叉验证：两次不一致说明至少一次裁错了。

## 落地到台账（求职 / 校招场景）

- 解出的链接**整条入台账**（含 `?referralCode=XXXX` 这类参数），并单列「内推码」字段；只写「有内推」等于没记。
- 记录后在聊天里**核对一句**：本人投递时用的是不是这个码 / 这个入口 —— 投递记录页往往只显示「已使用内推」而不显示码值，用错码等于白让出「优先筛选」。
- 两处分开存：图进 `1-情报\群消息与招聘会-MMDD\原图\`，链接与码值写进对应台账行的备注。
- 宣讲会现场给的**额外志愿 / 专属通道**这类口头情报，与码一起记进同一行备注，并标明「是否受志愿数上限约束」（不占上限＝直接投，占上限＝用它替换偏离主线的那个志愿）。

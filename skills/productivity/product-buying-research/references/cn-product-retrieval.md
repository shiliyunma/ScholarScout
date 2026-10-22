# 中文商品/技术资料的检索与抓取

配合 `product-buying-research` 主流程第 2 步使用。目标：拿到**型号名、件料型号、价格区间、拆解证据**，不追求一次抓全。

## 检索路线（按顺序换，别在一条路上重试到超时）

1. 内置搜索先上；中文查询超时很常见，**重试一次**即可。并发 3 个查询容易互相拖慢，改 2 个一批。
2. 仍然失败 → 抓 360 结果页，这是国内网络下最稳的无 Key 路径：

```python
url = "https://www.so.com/s?q=" + urllib.parse.quote(q)
# 结果块: <li class="res-list ...">...</li>
# 每块里: <h3>标题</h3>, href=跳转链, <p class="res-desc">摘要</p>
```

解析要点：摘要里往往直接带价格和件料（「XX 元」「欧姆龙 1 亿次微动」），足够生成候选表；标题里的数字经常比正文好使。

3. 需要正文 → 解析跳转壳拿真实 URL（见下），再按站点抓。

## 跳转壳解析（360 结果的 href 全是壳）

`https://www.so.com/link?m=...` 返回 200 + 约 300 字节，正文只有：

```html
<script>window.location.replace("https://real-url")</script>
```

用正则取 `location.replace\("([^"]+)"\)`（兜底 `URL='([^']+)'`），**带 `Referer: https://www.so.com/` 请求**；不带 referer 有时回空。拿到真实 URL 后再抓一次，并把真实 URL 写进交付物的来源列表——不要交跳转壳给用户。

## 站点可抓性（实测口径，会变，抓不到就换站点）

| 站点 | 情况 | 用法 |
|---|---|---|
| 中关村在线 `diy.zol.com.cn` / `mouse.zol.com.cn` | 文章页可抓，**编码是 GBK** | 解码顺序 utf-8 → gbk → gb18030；标题/报价/新闻都在正文 |
| 什么值得买 `post.smzdm.com` | 列表页可抓，**正文常是 JS 壳（返回 0 字节）** | 只用标题和搜索摘要当线索；正文换别站 |
| 太平洋 `product.pconline.com.cn` | 报价/参数页可抓，GBK | 拿参考价和参数 |
| 京东/天猫搜索页 | 对脚本 302 或验证页 | 不用来抓价；价格取文章里的「到手价」 |
| B 站/抖音 | 标题信息量极高（拆解型号、微动寿命常写在标题里） | 标题就够用，不必进视频 |

## 抓取模板

```python
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
raw = urllib.request.urlopen(req, timeout=30).read()
for enc in ("utf-8", "gbk", "gb18030"):
    try: s = raw.decode(enc); break
    except UnicodeDecodeError: continue
s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
txt = re.sub(r"(?s)<[^>]+>", "\n", html.unescape(s))
```

去标签后按关键词过滤（微动/编码器/传感器/回报率/元/g/电池），只打印命中的短行——整页正文进上下文没有价值。

## 判断资料可信度

- **一手**：官方参数页、拆解图/拆解视频标题（「欧姆龙 100M + TTC 金轮 + 3395」这种标题本身就是证据）、电商商品标题里的版本名。
- **二手可用**：垂直媒体（ZOL/太平洋/in 外设）的新闻与报价，带日期。
- **只当线索**：AI 聚合文章、搜索引擎 AI 摘要、带货软文。它们会把不同版本的参数混在一句里。

同一型号出现两个互斥参数时，先假设是**版本分档**，去找商品标题里的「标准版/Pro/Max」；证实是分档就把两档都写进表，别二选一。

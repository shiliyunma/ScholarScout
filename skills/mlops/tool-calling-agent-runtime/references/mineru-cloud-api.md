# MinerU 云 API（HTTP 服务）—— 文档解析 provider 的集成契约

Agent 的抓取/解析工具常要接第三方文档解析服务（扫描件 OCR、复杂版式）。这里以 MinerU 为例，
记的是**官方文档里看不出、必须实测才知道**的部分；换成别家也会撞到同类问题。

## 两条引擎

| | Agent 轻量 | 精准解析 |
|---|---|---|
| 认证 | **免 Token**（按 IP 限频） | `Authorization: Bearer <API Token>` |
| 限制 | ≤10MB / ≤20 页 | ≤200MB / ≤200 页 |
| 输出 | 仅 Markdown（CDN 链接 `markdown_url`） | Zip（内含 `full.md`、JSON） |

## 四个端点（提交 → 轮询）

| 方式 | 提交 | 轮询 |
|---|---|---|
| 远程 URL（Agent） | `POST /api/v1/agent/parse/url` | `GET /api/v1/agent/parse/{task_id}` |
| **本地文件（Agent）** | `POST /api/v1/agent/parse/file` | `GET /api/v1/agent/parse/{task_id}` |
| 远程 URL（精准） | `POST /api/v4/extract/task` | `GET /api/v4/extract/task/{task_id}` |
| **本地文件（精准）** | `POST /api/v4/file-urls/batch` | `GET /api/v4/extract-results/batch/{batch_id}` |

`state` ∈ `waiting-file / uploading / pending / running / converting / done / failed`；
`done` 给 `markdown_url`（Agent）或 `full_zip_url`（精准），`failed` 给 `err_code`+`err_msg`。

## 本地文件上传（“签名上传”）：提交 → PUT → 轮询

1. 提交：Agent `{"file_name": "x.pdf", "language": "ch", "is_ocr": true}`；
   精准 `{"files": [{"name": "x.pdf", "data_id": "..."}], "model_version": "vlm"}`；
2. 拿 `task_id` + `file_url`（Agent）或 `batch_id` + `file_urls[0]`（精准）；
3. **`PUT` 原始字节到预签名地址**（一个头都别多带）；
4. 轮询上表端点。

## 四个实测坑（换别家云解析也会撞到同类）

1. **`PUT` 预签名地址不能带 `Content-Type`** —— 它被算进签名，带了就 `403 SignatureDoesNotMatch`
   （实测：不带 → 200；带 `application/pdf` 或 `application/octet-stream` → 403）。
   同理别加 `Accept`/自定义头。封装成 `upload_bytes(url, body)` 并**不暴露 content_type 参数**，
   另加一条测试断言调用方不能传它。
2. **OCR 开关默认是关的**（`is_ocr=false`）：扫描件不显式打开就“上传成功、`state=done`、正文为空”——
   这种“成功但空”最难排查。
3. **优先上传手里的字节，而不是把 URL 交给对方去下**：URL 提交要求对方机器能访问到那个文件
   （内网地址、需登录、CDN 抖动、robots 都会失败）。字节已经拿到本地，直传少一个失败点。
4. **控制台给的 AK/SK 不一定是这个 API 的凭证**：先确认官方接口文档里到底要哪一种
   （MinerU 文档里 `Access Key`/`Secret Key` 出现 0 次，要的是 API Token；AK 当 Bearer 直接 401）。
   拿不准时**先用最短调用试一次**（只打印状态码与响应头行），别去猜整套签名方案。

## 失败回退与诚实报错

- 高精度接口失败（401/配额）要**自动退回轻量接口**，并且**轮询端点同步切换**——
  拿轻量接口的 `task_id` 去查另一个命名空间下的端点永远查不到。
- 两级失败时**两个原因都要留在结果里**（“直传 X；URL 提交 Y”），否则排障只知道“没成功”。
- 状态要分开表达：“服务暂不可用”与“文档确实没内容”是两件事，别都归成 `failed`。

## 怎么验“扫描件真能出正文”

自己没有扫描件就**造一份真的**（只改后缀不算）：用 pymupdf 建一页文字 → `get_pixmap()` 渲染成图 →
把图 `insert_image()` 进一个**新** PDF，然后

```python
check = pymupdf.open("scan-sample.pdf")
assert not "".join(pg.get_text() for pg in check).strip(), "还有文字层 → 这个‘扫描件’是假的"
```

**必须断言文字层为空** —— 否则测的是文字层提取而不是 OCR，假验证比不验证更糟。
实测：一页中文样本（约 6MB / 150dpi）走免 Token 直传，**10 秒级**返回可读中文 Markdown，
个别形近字误识（`l → 丨`）属正常。

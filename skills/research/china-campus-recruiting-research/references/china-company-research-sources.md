# Sources for Chinese employer / campus-recruiting research

Ordered by value per call. Extend this file when a new source class proves
useful; keep it topical, not per-company.

| Source | How to reach | What it yields | Caveats |
|---|---|---|---|
| 公司官网 (self-hosted CMS, `index.php?catid=NN` style URLs) | nav links: 公司简介 / 人才招聘 / 产品中心 / 公司新闻 / 解决方案; 招聘列表常分页 | Verbatim JDs (职责+任职资格), full product lineup, funding & honors, HQ addresses, target customers | 产品详情页与部分公告常为**图片海报**，innerText 为空；只取标题与列表 |
| 微信公众号 (`mp.weixin.qq.com/s/<id>`) | direct URL; read 标题 + 上一篇/目录 footer | 校招公告首发渠道、校招文章系列目录 | 正文多为图片海报 → 正文无文本；不要反复滚动重试，改用聚合站取同篇公告 |
| 校招聚合站·单篇 (超级简历 `wondercv.com/xiaozhao/<slug>/`, `hicv.cn/xiaozhao/...`) | search `"<公司> <届>春招"` then open the hit | **全文转载**：岗位表+方向说明、流程分步、投递邮箱与邮件标题格式、截止日、培养模式、FAQ | 收录时间在公告之后；以官网/公众号公告为准 |
| 校招聚合站·列表页 (批量岗位池) | `https://www.wondercv.com/xiaozhao/` ；翻页 `.../xiaozhao/page/pnN/?` | 每页 12 张岗位卡（卡片 = `a.campus-job-card`，字段 `.company` `.summary` `.card-tags .tag` `.card-date`），2–3 周可扫完上千条当季岗位 | **URL 查询参数不过滤**（`?q=` 静默忽略）→ 只能爬下来再用 Python 过滤；覆盖面是市场样本，不是全量 |
| 校招/求职聚合博客 ("公司与岗位分析" 类，常一次汇总十几家) | search `"<公司> <届>春招 启动"` | 公司简称、岗位清单、投递邮箱、公告日期 — 用于**发现**公告线索 | 二手解读，细节需回到一手公告核对 |
| **高校就业网 / 就业服务平台岗位页（最强薪资来源）** | 正方/zftal 式系统：单位页 `.../ckZpgwXq.zf?dwxxid=<单位ID>&zpxxbh=<岗位ID>`；岗位页 HTML 里用正则列出 `更多招聘职位` 的其余岗位链接 | **薪资档位（如「12000以上」）、学历要求、招聘人数、发布时间、全职/实习标记**，以及 单位规模/性质/行业/联系人/HR邮箱 | 薪资是平台下拉档位而非精确值；部分平台（杭电式）职位详情正文为空，只有标题+要求专业 |
| 高校就业网 / 学院公告 · 宣讲会 | search `"<公司> 校园招聘 宣讲会"` | 宣讲会时间地点、进校站点、当季真实启动时间 | 各校保留期不同；学院站常只留标题+公众号原文链接；聚合站会把「春招补录」错标成「秋招」 |
| 职友集 (`jobui.com/company/<id>/`) | search `site:jobui.com <公司全称>` | 规模、融资轮次、办公地址（多据点）、合作院校/客户、行业排名 | 工资/点评子页需登录 |
| 应届生求职网 / archiver 存档 | search `"<公司" 校园招聘` | 历年校园招聘岗位（可看岗位沿革与岗位族） | 多为存档年份，**不可当作当季岗位** |
| BOSS直聘 / 智联 / 猎聘 / 看准 | 搜索结果快照 | 岗位名、薪资区间、任职要求片段、公司在招岗位数 | 页面本身对匿名访问是空壳或登录墙 — 只读快照，不点进去 |
| 大型企业校招官网（直达投递，已实测可达） | 见下方清单 | 官方岗位库、流程页、宣讲行程、内推入口 | 部分站点 CDP 导航会被拦或超时 → 重试或用聚合站替代，并标注未验证 |
| **头条搜索 (so.toutiao.com/search?keyword=)** | `curl -s -m 25 -A "<UA>" "https://so.toutiao.com/search?keyword=<urlencoded>"` | **首选**：结果里内联来源页自身正文（高校就业网、聚合站、公众号转载），常直接带薪资数字与来源网站名 | 页面 1.8MB，前部全是 iframe 资源 URL — 只读正文段，别打印链接列表 |
| 360搜索 (so.com) | `https://www.so.com/s?q=<urlencoded>` | 中文公司名事实（规模/融资/工资/点评）片段覆盖最好；结果在 `li.res-list` 块内 | 结果链接是 `so.com/link?m=...` 包装；约 10–15 次查询后开始 302 空响应或验证码 |
| 手机360 (m.so.com) | `https://m.so.com/s?q=<urlencoded>` | 与桌面 so.com **独立的限流桶**，桌面被限后仍可用 | 同样会在大量请求后转验证码页 |
| 百度 / 搜狗 | — | 验证码拦截（首次请求即点选） | 直接跳过，不要消耗调用 |
| Bing CN | — | 中文公司名易被截断成单字并返回词典页 | 结果与查询无关即为该症状，换引擎 |

## 已验证的校招官网入口（投递直达）

招聘主站: 中国移动 https://job.10086.cn/（含算力/AI 校招专区）· 中兴 https://job.zte.com.cn/ ·
深信服 https://hr.sangfor.com/ · 奇安信 https://campus.qianxin.com/ ·
商汤 https://hr.sensetime.com/campus · 科大讯飞 https://campus.iflytek.com/

互联网: 腾讯 https://join.qq.com/ · 字节 https://jobs.bytedance.com/campus ·
美团 https://zhaopin.meituan.com/web/campus · 华为 https://career.huawei.com/reccampportal/portal5/campus-recruitment.html ·
快手 https://campus.kuaishou.cn/ · 网易 https://campus.163.com/ · 京东 https://campus.jd.com/ ·
小红书 https://campus.xiaohongshu.com/ · 58 https://campus.58.com/

牛客侧入口: 校招职位 https://www.nowcoder.com/jobs/recommend/campus · 面经合集
https://www.nowcoder.com/discuss/experience · 公司真题 https://www.nowcoder.com/contestRoom

> 站点改版常见；投递前重开一次确认，别把上季链接写进报告。

## 小型本土厂商（"招聘会上一个展位"）的取数顺序 — 实测高产出路径

小微/垂直厂商没有校招公告、没有品牌声量，常规搜索会全空。按下面顺序走，
一次能挖出完整画像：

1. **先找官网域名。** 官网域名常常**不在搜索结果里**，而是藏在第三方行业平台的
   「公司页」上：`surgi-plan.com` 式平台的公司页（`/company/view?id=N`）会放一个
   「官网二维码」，它是用 `jquery.qrcode` **客户端生成**的，域名以
   `text: "http://www.xxx.cn/"` **明文写在 HTML 的 JS 里** →
   `grep -o 'qrcode({.\{0,400\}' company.html` 或直接 grep `text: "http` 就能拿到。
   找那个平台公司页的办法：把 `/company/view?id=1..400` 跑一轮（每页 ~30KB，
   curl 循环 3 分钟），按 `<title>公司简介-<公司全名>` 命中。
2. **拿到官网后先读 `/job_chance.html`（或同义的「工作机会/人才招聘」页）**，
   小微厂商这里挂的是**常年有效、无批次、无截止日**的岗位，且**要求写得极具体**
   （某个工具链、某个编程语言、某年经验）—— 这是全站信息密度最高的一页，
   比任何聚合站都准。同时页脚和「联系我们」会给出**HR 邮箱 + 手机号**。
3. **官网新闻页是第二宝库**：专精特新/瞪羚/高新等荣誉文里带**申报批次与年份**，
   技术文里带**产品代号与研发路线**（如 TPS/ACS/VPS、「AI 质控平台」）。
   新闻条数会被截断（「共 N 记录」+ 分页），翻页拿全。
4. **招聘平台只取两条边界**：①官网岗位里没有的**销售/招商岗**（说明公司当下的
   扩张重心），②**公司页的工商信息块**（企业类型能直接看出是否为「法人独资」→
   有没有 100% 母公司）。**智联单条职位页 `/jobdetail/<id>.htm` 匿名可读全文**
   （岗位描述、福利、公司简介、工商信息全在里面），比公司页信息量大；
   公司页 `companydetail/CZ<id>.htm` 只渲染 3 条热招，其余需登录。
5. **真实规模看工商年报参保人数，不看招聘平台的规模标签。**
   招聘平台的「20-99 人」是企业**自填**，小微厂商普遍虚标一个数量级。
   权威顺序：**工商年报参保人数（水滴信用/爱企查）> 职友集 > 招聘平台自填**。
   反向用法：招聘会海报写「招 10 人」而年报参保 10 人 → 该公司一年要翻倍，
   值得问清是编制扩张还是纯提成制销售。

## 抓取机制上的坑（本次踩到）

- **头条搜索的响应不一定是 UTF-8。** 解成 UTF-8 会是满屏 `\ufffd` 乱码 —— 那是
  GBK 页被按 UTF-8 解码的典型症状，**不是查询没命中**。写一个 `try utf-8 → gbk →
  gb18030` 的兜底 decode，一次就修好。
- **职友集对匿名 curl 返回 0 字节**（公司页与主页都是），但带上 cookie jar
  （`-c cj.txt -b cj.txt`）+ `Referer` + 完整 `Sec-Fetch-*` 头后可正常取到公司页正文。
  先打一次首页把 cookie 种上，再请求公司页。
- **qcc.com（企查查）返回加密 shell**（几十 KB 的 `var arg1=` 乱码），直接跳过；
  改用搜索 snippet + 水滴信用 + 爱企查。
- **水滴信用 `shuidi.cn/company-<hash>.html` 匿名可读**，且是唯一一处稳定拿到
  **参保人数（含年报年份）** 与「实缴资本」的地方 —— 这两项洽好是判断小微厂商
  真实体量的关键字段。
- **高校就业网 / 企业 ATS 若是纯 JS 单页应用（91job 系、wrecruit 系），CLI 抓不到**
  也**不要开浏览器硬碰**（一次导航 + 等待就可能烧掉 7 分钟调用超时）。
  直接在报告里标注「为 JS 单页，需本人登录查看」比死磕划算。
- 搜索响应页面（1.7MB）**保存到临时目录再离线 grep**：一次页面往往值得三次检索，
  且重抓可能已被限流。

## 龙船招聘网（shiphr）——船舶/海工行业岗位枚举

`www.shiphr.com` 是船舶行业垂直站，公司页与岗位页**只有 WAP 版有内容**，桌面版是 JS 空壳：
- 岗位页：`https://www.shiphr.com/wap/c_job-a_comapply-id_<ID>.html`（标题即「岗位_公司名招聘-龙船招聘网」，正文含 地点/经验/学历/薪资/更新日/福利/招聘人数/接受应届生标记/职责原文）
- 公司页：`https://www.shiphr.com/wap/c_company-a_show-id_<ID>.html`（公司简介 + 福利清单；**在招职位列表常显示「该企业还没有发布职位信息」**，别据此判定该司无岗位）
- **站内搜索不可用**：`/wap/index.php?c=job&keyword=<公司名>` 返回「抱歉，没有找到信息」→ 用**外部引擎 `site:shiphr.com <公司简称> <岗位词>`** 反查页面，再逐个打开。
- **枚举方法**：换几组岗位词（工程师 / 主管 / 岗 / 各专业名）跑 `site:` 查询，用 `re.findall(r'shiphr\.com/(?:wap/)?c_job-a_comapply-id_(\d+)\.html', html)` 收 ID，去重后**批量取 WAP 页并只看 title 是否含公司名**（同页会混入别家的「推荐职位」，必须按 title 过滤）。实测一次能枚举出一家船厂 27 个在招岗位，含薪资栏之外的「接受应届生」标记。
- 薪资栏绝大多数是**「面议」**，只在福利栏披露结构（餐补/生日福利/五险一金/有年假/专车接送/补充医疗/年终奖/包食宿）；偶有历史岗带数值（如 4000-8000）。**别把「面议」当没数据**，福利清单本身就是可写进报告的待遇结构。
- 岗位表普遍写「性别要求：男」，属该行业显性特征，值得原样记录。

## 薪资调研补充

- `scripts/takehome_calc.py` 的 `CITIES` 表已补 **南京**（个人养老 8% + 医疗 2% +
  失业 0.5%；`si_cap` 取江苏社保上限粗值，**月薪低于约 24k 时该参数不生效**）。
  用法：`python scripts/takehome_calc.py 9000 13 南京 0.08 0`。
- 小微厂商「官网岗位无薪资」是常态 → 别把它当数据获取失败；改从**同城同类岗**
  （职友集按学历统计的 ￥X K）+ **同公司销售岗已公示的薪资带**两端夹逼，
  并明确标注为**推算**。

## Aggregator list crawl recipe (岗位池批量筛选)

1. Open the list page, confirm card count and the pagination link shape from the DOM
   (`a.campus-job-card`, `.../page/pnN/?`) — do not guess pagination from the footer text.
2. Loop pages in bounded batches (~50–60 pages per browser call) and append each batch
   to `$BH_AGENT_WORKSPACE/<name>.json`, with the page number on every record. If the
   run dies mid-way, resume at the next page number in a fresh session — never restart
   from page 1.
3. Return a JSON string from the extraction JS (`JSON.stringify(rows)`) and `json.loads`
   it in Python; do the filtering/sorting/dedupe in Python, not in JS.
4. Filter with keyword families derived from the resume (AI Agent/大模型/Agent/AI应用、
   Java/后端/软件开发、算法/机器学习/NLP、C++/嵌入式、测试开发/数据开发) plus the
   cities worth relocating to; then group the survivors by 匹配度 for the report.
5. Re-open a sample of the surviving detail pages (and any single link you will quote)
   before shipping them.

## Browser mechanics that matter

- Build queries with `encodeURIComponent` (query strings with CJK).
- Extract result anchors on engine pages by filtering `a.innerText` for the
  company name, then `goto_url(anchor.href)`; engine result links are wrappers.
- Job boards and 微信 pages frequently need no scrolling but have empty JS text;
  prefer sources whose text layer is real HTML (official site lists, aggregator
  article pages, school pages).
- **Slice text in Python, not inside the JS expression**: `js()` takes one JS
  expression, so `js("document.body.innerText...[:1500]")` fails with a SyntaxError —
  return the string, then slice it in Python.
- When climbing the DOM for "the card text", stop at the card element itself
  (`a.campus-job-card`); walking 4 ancestors up lands on the page wrapper and every
  record ends up with identical text.
- Keep each batch append-only when the table gets long: several medium calls beat
  one giant extraction that risks a timeout.

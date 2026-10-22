---
name: resume-docx-surgery-zh
description: Use when 改中文简历 docx 并出交付 PDF。含文本框同步与证据链。
---

# 中文简历 docx 手术 + 交付 PDF

适用于「超级简历/wondercv 系」中文简历模板：内容全在固定尺寸文本框里，改的是字，**不能碰版式**。

## 1. 先侦察，别急着改

解 `word/document.xml`，按 `<w:p>` 切段、按 `<w:r>` 切 run，打印**段号 + run 数 + bold/sz/color + 文本**。
先跑探针脚本看清「哪个框、第几段、由几个 run 组成」，再动手。

模板家族事实：
- **每一行都存两份**：`mc:Choice`(wps) + `mc:Fallback`(VML)。**两份必须同步改**，否则 Word 与
  其它渲染器显示不一致。
- 样式在 run 级：`w:rPr` 里 微软雅黑 / `w:sz w:val="18"`(9pt) / color `414141`；
  粗体 run 与普通 run 只差 `<w:b/>`。
- **同一段文字可能落在多于一个 `mc:AlternateContent` 块里**。**不要「只改第一个匹配到的块」**——
  实测会漏掉后一份。要么按 `<w:t>` 精确文本全局替换，要么遍历所有块并断言
  「替换总数 == 改前出现总数」。
- 各框为 **`noAutofit` + 固定 `a:ext`**（项目经历框实测 `cx="6429375" cy="3238500"` EMU）：
  **删一个条目会在框底留 3–4 行空白，补回内容后空白自然消失**。
- 🔴 **容量估算器的百分比偏乐观，不要按它判余量**：按字符宽度算的 `est height used: N% of M pt`
  在框**实际已满**时仍报 70–75%（实测报 184/248 pt = 74%，再加一行就溢出）。
  **判读规则：≥65% 一律当满分对待**；「还能不能加字」用「同框已排好的现有行数」去推，别用这个百分比。
- 🔴 **往已满的框里加字，必须在同一个框里删掉等量文字。** 零损失可删项优先挑**同框内重复**：
  正文开头重述标题括注的句子、正文末尾重述「荣誉 / 奖项」栏的句子。判据是「删掉后该信息在别处仍出现一次」。
  **净增控制在 ±1 行（约 ±50 字）以内**，行数才与已验收的旧版一致。

## 2. 改字：只换 `w:t` 内层，保留外层 `w:r`/`w:rPr`

```python
pat = re.compile(r'(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)', re.S)
def sub(m):
    return m.group(1) + esc(new) + m.group(3) if html.unescape(m.group(2)) == old else m.group(0)
xml, n = pat.subn(sub, xml)
assert n == before, (before, n)      # 改前几处就必须替换几处
```

⚠️ **子串断言会假通过**：`assert xml.count(new) == 2` **不可靠** —— 当 `new` 是 `old` 的前缀时
（`github.com/x` 是 `github.com/x/raglearn` 的前缀），**没被替换的那份 `old` 里也含 `new`**，
计数照样是 2，于是「改完了」的检查通过而实际漏改一处。
必须同时断言：① **`old not in xml`**；② **`new` 的 `<w:t>` 全等出现 N 次**（逐条 `html.unescape`
后全等比对，**不做子串计数**）。

### 2.1 整段重写：不换子串，按 run 重建

要点的增删幅度大时不要做多次子串替换——**用该段自己的 run 当模板整段重建**：

```python
rs   = runs_of(para)                  # 段内全部 <w:r>
plain = rs[4][2]                      # 取一个非粗 run 当普通模板
bold  = rs[3][2]                      # 取一个粗 run 当加粗模板（先 assert 'w:b' in bold）
head = para[:rs[0][0]]                # 保留 <w:pPr> 与起始标签
tail = para[rs[-1][1]:]               # 保留 </w:p> 等收尾
segs = [(False, "…"), (True, "加粗…"), (False, "…")]      # (是否加粗, 文本)
new  = head + "".join(clone_run(bold if b else plain, t) for b, t in segs) + tail
assert t_of(new) == "".join(t for _, t in segs)
```

- **只重建要改的那一段**，其余各段 `assert t_of(...) == 改前文本` 逐段核。
  段落计数断言抓不到「删两条补两条」，必须逐段比文本。
- 重建后复核段落数不变：`len(para_spans(new_inner)) == 改前段数`。
- `clone_run` 只替换 run 里**第一个** `<w:t>`。模板 run 若含第二个 `<w:t>`，旧文本会残留
  —— **靠 `assert t_of(new) == 期望全文` 兜住**，只看替换计数会漏。

### 2.2 跨版本搬运条目 / 拼接重建整框

用户说「从 vNN 把 X 那段找回来，**大致框架不要变**」时，**不要照自己的记忆重写那段** ——
从旧版 docx 里把那一条的**整段 XML 原样抽出来**（标题段 + 正文段，含 run 级 `rPr`），插进当前版：

```python
sp = para_spans(old_inner)
assert idx_of_title == [4]                        # 先断言段号，别靠文本匹配猜
title_xml, body_xml = old_inner[sp[4][0]:sp[4][1]], old_inner[sp[5][0]:sp[5][1]]
sep = cur_inner[sp[1][1]:sp[2][0]]                # 复用当前版的「段间分隔」原样字节
new = inner[:sp[1][0]] + p1_new + sep + p2 + sep + p3 + sep + title_xml + sep + body_xml + tail
```

- 🔴 **`head` 的切点是最容易错的一刀**：`inner[:spans[0][0]]` 是「第 0 段之前」，**不含第 0 段**
  —— 拿它当 head 会把第一条目整段丢掉（实测：首条的标题连同日期一起消失，只有逐段核文本才发现）。
  正确切点是 `inner[:spans[1][0]]`（＝第 0 段 ＋ 其后分隔）。**切完立刻断言
  `t_of(head).startswith(<首条标题>)`**，标题在 head 里才算对。
- `mc:Choice` 与 `mc:Fallback` **各自从旧版对应的一份里抽**（Choice←Choice、Fallback←Fallback），
  混搭会让另一份渲染器拿到不匹配的样式。
- 搬运后**断言与旧版逐字一致**：标题串与正文串在新文档里的出现次数都 == 2（两份副本），
  这是「原样搬回」唯一可核验的证明。
- **搬回来的条目常常正好把「删条目留下的框底空白」填满** —— 这通常就是「大致框架不要变」的真实含义，
  做完顺手确认框底空白已消失，并在改动说明里写这一条。

### 2.3 删条目 / 并入条目

**删一条（标题段 + 正文段）**：不要逐段删 —— 一次切掉 `[标题段首, 下下条段首)`，这样连带删掉中间的分隔，
**前一条留的那份分隔成为唯一的段间分隔**：

```python
ki = [i for i, (a, b) in enumerate(spans) if t_of(inner[a:b]).startswith(<要删的标题>)]
assert ki == [4], ki                                       # 先断言段号，不用模糊文本匹配
cut_from, cut_to = spans[ki[0]][0], spans[ki[0] + 2][0]    # +2 = 跳过它自己的正文档
new = inner[:cut_from] + inner[cut_to:]
spans2 = para_spans(new)
assert len(spans2) == 改前段数 - 2
assert new[spans2[3][1]:spans2[4][0]] == inner[spans[3][1]:spans[4][0]]   # 留下的分隔字节未变
```

**把一条独立条目并入另一条的正文**（用户说「能不能放进 X 里面？不行就不加」时）：
用**目标段自己的 run 当模板**追加（别新建 run），加完做一次「减法断言」证明原内容没动：

```python
pb2 = pb[:rs[-1][1]] + clone_run(plain_tpl, A) + clone_run(bold_tpl, B) + clone_run(plain_tpl, C) + pb[rs[-1][1]:]
assert t_of(pb2).replace(A + B + C, "") == t_of(pb)      # 去掉新加的三段后与改前逐字相等
```

- 🔴 **「能不能并」要有依据，找不到依据就不加**（用户原话「不行就不加」）：去旧版本里查该条目**是否本来
  就与目标条目同源**。判据是旧版该条目的标题里是否写着两者是一件事（本次：旧版毕设条标题就是
  「…基于 <框架> 架构的运行时实现」，说明框架研读本来就是毕设的一部分，并进去是**还原**而非包装）。
- **删/并条目会连带改掉该行自己的填充 run**：`w:t` Counter diff 里会看到 `'         '`（多个空格的居中填充）
  与全角空格 `'　'` 的计数**成对下降**，那是被删那一行的组成部分，**不是「动到别处」**。
- 删/并之后要重跑一遍框底目视：删条目会在 `noAutofit` 框底留 3–4 行空白，并入/搬回内容往往正好填满它。

## 3. 「其余一字未动」的硬证据：`w:t` Counter diff

每次构建都打印改前/改后每条 `w:t` 的出现次数差。**diff 里只应出现预期的那几项** —— 这是唯一
可核验的证据，比「我记得没动」强。

其它断言：段落数（如 8 → 6）、保留内容逐字仍在（标题/日期/其它条目）、`ET.fromstring` 过 XML。
**先断言再写文件**，断言不过就不落地。

## 4. 版本链

- **改动一律新建文件**（v11 → v12 → v13），**旧版原样保留、永不覆盖**。
- 每版落一份 `附-<版本>改动说明.md`：改动表 + **明确未改动清单** + 校验结果 + 遗留问题。
- 底稿永远选「上一版产物」而不是更早的源版本，这样「其它地方」天然字节级不变。

## 5. 交付 PDF：一律 Word COM，LibreOffice 只作预览

两个渲染器**断行位置不同**（同一行放不下与否会不一致），预览可用 LibreOffice，**交付稿必须 Word 导**。

```powershell
# .ps1 必须存 UTF-8 **BOM**，否则 PowerShell 5.1 按 ANSI 读、中文路径乱码、Documents.Open 抛 COMException
$ErrorActionPreference='Stop'
$w = New-Object -ComObject Word.Application; $w.Visible=$false; $w.DisplayAlerts=0
$d = $w.Documents.Open($src, $false, $true)        # 第三参 True = 只读打开
$d.ExportAsFixedFormat($out, 17)                   # 17 = wdExportFormatPDF
Write-Output ("PAGES=" + $d.ComputeStatistics(2))   # 2 = wdStatisticPages
$d.Close($false); $w.Quit()
```

**产出这类 .ps1 最可靠的方式：复制上一版已跑通的脚本，用 `sed 's/vNN/vMM/g' 旧.ps1 > 新.ps1` 改版本号**
—— `sed` 按字节处理，**BOM 与中文路径原样继承**；用写文件工具直接生成的脚本常常没有 BOM。
执行时 `cd` 进脚本所在目录、用**相对 ASCII 文件名**调用（`powershell -ExecutionPolicy Bypass -File toword_pdf_vMM.ps1`），
不要把中文绝对路径放到命令行上再被转一次码。

导出前先 `tasklist 2>/dev/null | grep -i winword` 确认没有 Word 在跑。

验收（**前两项都不算通过标准，只有目视算**）：
① `PAGES=` 与 `fitz.open(pdf).page_count` 一致；
② `/Producer` 含 **Microsoft Word**（PDF 里是 UTF-16BE，形如 `b'Microsoft\xa9 Word'`），不是 LibreOffice；
③ **目视**：渲成 PNG（`get_pixmap(dpi=150)`）后**逐条读框内每一段的最后一句**，确认都以句号收尾；
   整页看不清时只裁框底那一小条放大看；
④ 构建脚本的 XML 断言 + `w:t` Counter diff 只出现预期项。

🔴 **两条会骗过你的假信号**：
- **`PAGES=1` 不代表没被裁**：`noAutofit` 框溢出的实际处理是**静默裁掉最后一行**，页数照报 1。
- **PDF 文本层里有那行字，不代表它被渲染出来了**：被裁的行仍在内容流里，`extract_text()` / grep
  都能命中 → 拿文本层核对会得出「完好」的错误结论。**渲图目视是唯一的验收手段。**

## 6. 外发

发给用户时**同时给一份 ASCII 文件名副本**（`ZhangSihao-Resume-v13-AI-Agent.pdf`）：
微信上传对非 ASCII 路径偶发失败。

### 6.1 投递前先导一份「正文纯文本」（也当存档）

需要把简历正文发到聊天渠道、或给用户一份可直接粘贴的纯字符版时，**不能直接遍历 `w:p`/`w:t`**——
文本框的两份副本会让每个字段出现两遍，并排字段还会被读成一整段。做法：

- **先剥 `mc:Fallback` 子树再抽文本**：`Choice` 与 `Fallback` 是同一份内容的两个副本；用 `ElementTree`，
  把 tag 为 `{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback` 的元素整体删掉。
- **粘合段要丢**：判据是「一段里出现 ≥2 个字段标签（`求职意向：`+`年 龄：`…）」或「≥2 个栏目名
  （基本信息/教育背景/项目经历/…）」或「长度 > 400 字」，三种任一即丢（真 bullet 到不了 400 字）。
- **同段文本再去重**（`seen` 集合），落 `<版本>-正文纯文本.txt`；**先打印行数/字符数与全文**，
  肉眼确认没有粘合段与重复段再发。

### 6.2 投递命令（Hermes 通道）

```bash
hermes send --list                     # 权威收件人清单：platform:chat_id (dm)
hermes send --to <platform>:<chat_id> --subject "简历 v12" --file <纯文本.txt>
hermes send --to <platform>:<chat_id> "MEDIA:/abs/path/resume.pdf"   # 发附件
```

- **收件人 id 一律以 `hermes send --list` 的输出为准**，不要凭记忆拼。
- `--file` 读正文；`MEDIA:<绝对路径>` 发附件。附件要走平台的图片/文件 CDN，网络受限时可能失败——
  **失败就改发文本正文，并把 PDF 的绝对路径交给用户自己转发**，不要反复重试。

### 2.4 用 python-docx 改（而不是裸 XML）时的三个雷

只改文字时 python-docx 比裸 XML 省事，但它对文本框的处理有两个"静默毁版"行为：

- **`document.paragraphs` 看不见文本框里的字**。文本框内容在 `<w:txbxContent>` 下，不在 body 的段落流里 ——
  用 `paragraphs` 遍历会得出"文档几乎是空的"这种错误结论。要碰文本框必须显式遍历
  `doc.element.body.iter(qn("w:txbxContent"))`。
- 🔴 **给段落赋 `.text = ...` 会重建该段所有 run**。若那段正好是文本框的宿主段，赋完值**整个文本框会消失**
  （实测 `w:txbxContent` 计数 1 → 0，且不报错）。规则：**只改 `run.text`，绝不给 `paragraph.text` 赋值**；
  要整段重写就走 §2.1 的 run 重建法（或裸 XML）。
- 混合格式段落要逐 run 改：先打印 `[(r.text, r.bold, r.font.size)]`，按 run 改，别把整段并成一个格式。

选择判据：只改文字 → python-docx；要保住文本框 / 字段 / 关系文件 → 裸 XML。无论走哪条，改完都回读
**`w:txbxContent` 数量 == 改前**，这是"没把版式改坏"的最便宜证据。

## 坑

- LibreOffice 的 stdout 用 `text=True, capture_output=True` 会在 GBK 报错时 `UnicodeDecodeError`
  → 从 shell 跑，或不加 `text=True`。
- 只在某个块里做替换的脚本（`find` 第一个匹配块）**会静默漏一份**，务必用全局替换 + 计数断言。
- 只要改了一份副本（`Choice` 或 `Fallback`）就交付，渲染器不同则显示不一致；**交付前断言两份都改了**。
- 用户说「某处不许动」时，**要能拿出证据**：Counter diff 里那一项计数不变，就是证据。
- **「页数正常 + 文本层能搜到」是假阴性组合**：`noAutofit` 框溢出被静默裁行，页数与文本层都不报警。
  改过固定高度框之后，**必须渲图逐段读末句**。
- **框满了不要指望调字号救**：字号一改整框行数全变，反而更难与已验收的旧版核对；优先「同框删重复」。
- **交付 PDF 的 `/Producer` 要在 PDF 字节里核**：字符串被压缩进对象流时，`grep` 只命中字体名会
  产生"查不到"的错觉；用 `fitz` 读元数据而不是自己的正则。

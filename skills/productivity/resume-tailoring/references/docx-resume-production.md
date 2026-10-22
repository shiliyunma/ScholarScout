# 把简历产出成「实测 1 页」的 Word / PDF

md 是源文件；投递要的是 Word/PDF。「1 页」必须是**量出来的**，不是估的 —— 估出来的 1 页在对方机器上、
换台打印机、或被 HR 的编辑器重新分页后就变成 2 页。

## 生产流程

1. **内容写成数据结构，不要写死在排版代码里**：`BLOCKS = [(kind, text), ...]`，`kind ∈ {H 区块标题, E 条目+右对齐日期,
   T 技术栈, B 要点, P 普通行}`。之后每次改内容只动数据，排版逻辑一行不用碰。
2. **`build(body_pt, line_spacing_pt, margin_cm, photo_w_cm, out_path)`** —— 把所有可调排版参数显式化。
3. **自动试参定页**：从「易读 → 紧凑」枚举候选参数，每个生成 → 转 PDF → 数页数，取**第一个刚好 1 页的**
   （= 满足页数约束的最大可读字号）。这一步把「1 页」从猜测变成事实。

   ```python
   CANDIDATES = [(10.0,13.0,1.4,2.6), (9.5,12.5,1.35,2.5), (9.0,11.5,1.27,2.4), (8.5,10.5,1.2,2.3)]
   for bp, lp, mg, pw in CANDIDATES:
       build(bp, lp, mg, pw, probe)
       if len(PdfReader(to_pdf(probe)).pages) == 1:
           chosen = (bp, lp, mg, pw); break
   ```
   全部候选都超页时，**退回最紧参数也要产出文件**（半成品好过没有），并明确告诉用户「还需要砍内容」。
4. **转 PDF（LibreOffice 无头）**：

   ```python
   cmd = [SOFFICE, "--headless", f"-env:UserInstallation={prof_url}",
          "--convert-to", "pdf", "--outdir", TMP, docx_path]
   ```
   - `prof_url` **必须是正斜杠** `file:///C:/Users/...`。写成 native 反斜杠路径会**静默不产出 PDF**
     （命令返回 0，目录里却什么都没有）—— 这是「转换没结果」最常见的原因。
   - 用独立 `UserInstallation` 避免与用户正在运行的 LibreOffice 冲突；另备一条**不带 `-env`** 的 fallback。
   - 每次转换前清掉输出目录里旧的 `*.pdf`，否则会读到上一次的结果。
5. **数页数**：`pypdf.PdfReader(path).pages`。
6. **报告实测值**，不是估算：`正文 Xpt / 行距 Ypt / 页边距 Zcm / 实测 N 页`。
7. **同时交付 PDF 和 docx**：PDF 让用户验收排版，docx 让他自己改措辞。
8. **换环境先确认解释器**：默认 `python` 可能没装 `python-docx` / `pypdf`。本机上别的第三方项目 venv 里往往有，
   直接用它跑（`<venv>/Scripts/python.exe build.py`）比自己装快。

## python-docx 版式坑（每条都真实吃掉过一整轮）

- **默认页面是 US Letter，不是 A4。** 默认模板是 21.59×27.94cm。必须显式设置：
  ```python
  sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
  ```
  WHY 要紧：Letter 比 A4 **矮 1.76cm**。在 Letter 上刚好 1 页的排版换到 A4 会突然溢出；反过来你会因为 Letter 更矮
  而**误以为必须用更小的字号**。先定 A4 再调参，否则整轮试参白做。
- **`cell.width` 单独设了没用。** LibreOffice/Word 会忽略它、按内容自动重排列宽，症状是**文字在奇怪的位置折行**
  （例如把「方向）」折到下一行）。必须固定表格布局 + 显式列宽：
  ```python
  tbl.autofit = False                       # python-docx 会写入 tblLayout=fixed
  for gc, w in zip(tbl._tbl.find(qn('w:tblGrid')).findall(qn('w:gridCol')), widths_cm):
      gc.set(qn('w:w'), str(int(w * 567)))   # 1 cm = 567 twips
  for cell, w in zip(row.cells, widths_cm):
      cell.width = Cm(w)
  ```
- **「固定行距」会把内嵌图片裁掉。** `line_spacing_rule = WD_LINE_SPACING.EXACTLY` 配一个 11pt 行框，会把 3.6cm 宽的
  校徽**纵向裁成一条**（只剩上半截）。含图片的段落必须用单倍行距，让行框自己长到图片高度：
  ```python
  p.paragraph_format.line_spacing = 1.0
  p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
  ```
- **`space_before` 不接受负值**（`ValueError: value must be in range 0 to …`）。想把照片放到文字右侧，别用负偏移，
  改成**多开一列表格**，让照片自然落在右侧单元格里。
- **先设列宽，再合并单元格。** 合并后 `row.cells` 会把合并后的单元格重复返回，逐个赋宽会互相覆盖、把 tblGrid 弄乱。
- **行的合并范围要覆盖所有内容列**，否则尾部列单独留着，表现为右侧一条多余空白。
- **行内加粗别用正则。** 用正则匹配「数字」会把 `Vue2`、`Top-20` 一起加粗。改成**显式列举要加粗的词条**：
  ```python
  for t in BOLD_TERMS:
      if t in text: text = text.replace(t, f"**{t}**")
  ```
  渲染交给一个 `add_rich()`：按 `\*\*(.+?)\*\*` 切分，奇数段加粗（字号可 +0.4pt 增强对比）。
  同一个 `mark()` 里顺便收紧分隔符空格（` A ｜ B ` → `A｜B`），别在多处各改一遍。

## 给定模板文件时的产出方式（填模板，不是重建）

用户给一份「按这个来」的 docx / PDF 时：

1. **颜色从渲染图取，不要靠眼睛或 vision 判断。** 源文件转 PDF → Ghostscript 渲染 PNG → 统计像素。
   Vision 会把 `#4E7282` 钢蓝说成「青色」，据此配色一定偏。同一个统计还能给出**几何**：把某颜色的像素按行聚合成
   带，就能量出标题条的**高度**与**横向范围**，直接换算成 pt / cm。
   ```bash
   gswin64c -q -dNOPAUSE -dBATCH -sDEVICE=png16m -r150 -sOutputFile=src-%d.png src.pdf
   ```
2. **源文件里的证件照等内嵌图**用 pypdf 直接抠出来复用：
   ```python
   for im in PdfReader(pdf).pages[0].images: open(im.name, 'wb').write(im.data)
   ```
3. **填模板，不要「按视觉规范重建」。** 用户说「以 X.docx 为模板」「格式完全与 X 保持一致」时，唯一正确的做法是
   **把这个文件本身填上内容**：形状、位置、配色、标签块、装饰元素一个不动，只替换文本框里的文字。按模板的视觉规范
   另建一份新文档**会被明确退回**（「格式不对的」）—— 重建版单看挺像，但绝对位置与装饰几何对不齐，用户一眼认出不是同一份。
   用 lxml 把所有文本框映射出来（`root.findall(".//{…wordprocessingml…}txbxContent")`）。
   **`mc:AlternateContent` 让每个文本框出现两次**（Choice/Fallback）—— 40 个其实只有 20 个逻辑文本框，**两份都要改**。
4. **标注目标框要用「不含内部空格的独特键」，并在脚本里打印「已处理的框」清单。** 中文模板的标签常在字之间夹空格
   （`姓　　名：`、`手　　机：`），拿 `"姓名"` 这样的键去匹配会**静默匹配不上** —— 不报错，框里旧内容原样留着，
   肉眼验收交付物时极易漏掉。改用 `政治面貌`、`上海财经大学`、`CET-6（620分）` 这类无空格键，并把命中的框名打出来逐项核对。
5. **内容装不下 = 压内容去适配固定尺寸的框，不要动几何。** 依次用：① 每行压成**不折行的单行**（模板框宽约容
   55–75 个全角单位，超了就折行、折出来的行会顶爆框）；② 按区块单独调字号（密集区块 8.0pt、其余 8.5pt）；
   ③ **把克隆段落的段前/段后间距清零并设固定行距** —— 模板段落自带间距，克隆 N 段后累计能撑爆一个框，这是最隐蔽的一条。
   还装不下就回来找用户砍内容。
   - 清零间距写 `w:spacing` 的 `before/after=0` + `line/lineRule=exact`，而**插入 `w:pPr` 子元素必须按 schema 顺序**
     （`w:spacing` 在 `snapToGrid` 之后、`ind` 之前）。直接 append 到 pPr 末尾会让 Word 认为文档损坏。
6. **绝对定位的文本框溢出 = 文字重叠，不是翻页。** 溢出的文字直接压在下一个区块上，**文件依旧只有 1 页** ——
   所以页数校验**完全不能**证明排版没坏。必须渲染 PNG 走 vision，专门问「有没有文字互相重叠 / 有没有文字压到下一个区块标题上」。
   叠字是本类任务唯一的真实失败模式，两次都只有看图才发现。
7. **模板的章节槽位可以改名复用**：模板里是「实习经历 / 校园经历」，你要的是「项目经历 / 实习经历」——
   标签只是文本框里的文字，改字即可，几何不用动。

## 读「批注版」指导文件

用户给一份带手写批注 / 批改意见的 PDF：

1. **先查有没有真正的 PDF 批注对象**：`PdfReader(pdf).pages[i].get('/Annots')`。经 LibreOffice 导出的「批注版」
   常常是 **0 个批注对象** —— 那些红字是**画在页面上的像素**，不是注释。
2. **`read_file` 可能直接报 `NeedsOcrError`**（无文本层）。别在这里纠结，直接渲染成 PNG 走 vision。
3. 需要逐字引用时用 tesseract（`-l chi_sim`）；但**手写体 OCR 不可靠**，关键条目要用 vision 放大到局部
   （`vision_analyze(..., region=[x1,y1,x2,y2])`）**逐字读第二遍**再落地 —— 要据以拒绝某条建议时，读错原文很难看。
4. 读完后**逐条列成表**（批注原文 | 处理），拒绝的单独分出「不改 + 为什么 + 替代方案」。

## 交付前的视觉核验（必做）

数值验证（页数 / 字号 / 色值）过了不等于排版没问题 —— **折行断字、图片被裁、元素错位只有看渲染图才发现**。

```bash
gswin64c -q -dNOPAUSE -dBATCH -sDEVICE=png16m -r150 -dFirstPage=1 -dLastPage=1 -sOutputFile=out.png out.pdf
```

然后 vision 逐项问：标题有没有折行、断在哪个字后面、有没有元素被裁、区块标题是否高度一致、内容有没有出页。
**顺手用像素再核一遍关键几何**（某颜色的 y 带范围、照片的宽高比），比 vision 的描述可靠。

只有看图才会发现的典型问题：长标题在「校级铜奖」中间被折成两行（→ 把奖项拆成独立 bullet，零额外行数）、
校徽被固定行距裁成一条（→ 改单倍行距）、信息行在「方向）」处折行（→ 修表格列宽）。

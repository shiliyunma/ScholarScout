#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fixed-layout .docx textbox: measure capacity, then rewrite a block keeping the template's own styles.

Why this exists
---------------
In a fixed Word template (e.g. A-01.docx) the body text sits in absolutely-positioned
boxes (``w:txbxContent``).  Overflow does NOT create a page: the extra line is drawn
over/under the next colour band and is simply lost -- while the page count stays 1.
So measure the box BEFORE writing, and render a PNG AFTER.  Parsing the paragraph's
run structure by eye is not needed: extract the existing ``rPr`` and reuse it.

Commands
--------
  probe   SRC.docx                        geometry + capacity of every textbox, with a preview
  width   "文本…" [--size 9]              rendered width in pt and lines needed at that size
  rewrite SRC.docx DST.docx SPEC.json     replace the matching paragraph(s), keeping pPr/rPr

SPEC.json
---------
  {
    "match":  "参与一套数据管理",   # substring that lives INSIDE A SINGLE RUN (see pitfall)
    "tail":   "如期交付",           # optional: extra substring that must also be present
    "expect": 2,                     # mc:Choice + mc:Fallback = 2 copies; mismatch aborts
    "segments": [["普通文字，", false], ["加粗文字", true], ["。", false]]
  }

Pitfall that costs the most time
--------------------------------
A paragraph is split across many runs, so using the whole paragraph text as ``match``
gives ZERO hits and the file is written unchanged (silently, if you do not assert).
Always set ``expect`` and check the reported count.
"""

import argparse
import difflib
import json
import re
import sys
import zipfile

EMU_PER_PT = 914400 / 72.0

# ---------------------------------------------------------------- text measurement


def char_pt(ch, size=9.0):
    """Advance width in pt for one char in a CJK font at `size` pt.

    Calibrated on 微软雅黑: a rendered line of mixed Chinese/ASCII filled 97% of the
    predicted width, so these ratios are good enough to count lines.
    """
    if ord(ch) > 0x2000:          # CJK + fullwidth punctuation
        return size
    if ch == ' ':
        return size * 0.29
    if ch.isdigit() or ch.isalpha():
        return size * 0.53
    return size * 0.33             # ascii punctuation


def text_width(text, size=9.0):
    return sum(char_pt(c, size) for c in text)


def line_count(text, usable_w, size=9.0):
    if usable_w <= 0 or not text:
        return 0
    return int(-(-text_width(text, size) // usable_w))   # ceil


# ---------------------------------------------------------------- box geometry

ANCHOR_RE = re.compile(r'<wp:anchor|<wps:wsp|<w:pict|<w:drawing')
EXT_RE = re.compile(r'<wp:extent cx="(\d+)" cy="(\d+)"/>')
BODYPR_RE = re.compile(r'<wps:bodyPr[^>]*>')
TXT_RE = re.compile(r'<w:t[^>]*>(.*?)</w:t>', re.S)


def _ins(bodypr, key, default):
    m = re.search(key + r'="(\d+)"', bodypr or '')
    return int(m.group(1)) if m else default


def boxes(xml):
    """Yield every txbxContent with the geometry of the shape that owns it."""
    out = []
    for m in re.finditer(r'<w:txbxContent>', xml):
        s = m.start()
        seg = xml[max(0, s - 40000):s]
        cuts = [mm.start() for mm in ANCHOR_RE.finditer(seg)]
        seg = seg[cuts[-1]:] if cuts else seg          # keep only the enclosing shape
        ext = EXT_RE.findall(seg)
        bp = BODYPR_RE.findall(seg)
        cx, cy = (int(ext[-1][0]), int(ext[-1][1])) if ext else (0, 0)
        bodypr = bp[-1] if bp else ''
        l, r = _ins(bodypr, 'lIns', 91440), _ins(bodypr, 'rIns', 91440)
        t, b = _ins(bodypr, 'tIns', 45720), _ins(bodypr, 'bIns', 45720)
        end = xml.find('</w:txbxContent>', s)
        inner = xml[s:end]
        out.append({
            'start': s,
            'box_pt': (cx / EMU_PER_PT, cy / EMU_PER_PT),
            'usable_pt': (cx / EMU_PER_PT - (l + r) / EMU_PER_PT,
                          cy / EMU_PER_PT - (t + b) / EMU_PER_PT),
            'text': ''.join(TXT_RE.findall(inner)),
        })
    return out


def normalize(s):
    return re.sub(r'\s+', '', s)


def read_docx(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
    return names, blobs


def write_docx(dst, names, blobs):
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
        for n in names:                     # keep original entry order; [Content_Types].xml first
            z.writestr(n, blobs[n])


def all_text(blobs):
    return ''.join(TXT_RE.findall(blobs['word/document.xml'].decode('utf-8')))


# ---------------------------------------------------------------- commands


def cmd_probe(args):
    _, blobs = read_docx(args.src)
    xml = blobs['word/document.xml'].decode('utf-8')
    bs = boxes(xml)
    print('%-4s %-14s %-16s %-10s %s' % ('#', 'box pt (w x h)', 'usable pt', 'lines fit', 'preview'))
    for i, b in enumerate(bs):
        uw, uh = b['usable_pt']
        fit = int(uh // 11.9) if uh > 0 else 0        # ~11.9pt per line at 9pt single spacing
        print('%-4d %-14s %-16s %-10s %s' % (
            i, '%.0f x %.0f' % b['box_pt'], '%.1f x %.1f' % (uw, uh),
            '~%d' % fit, normalize(b['text'])[:34]))
    print('\nnote: "lines fit" is geometry only. The real limit is where the NEXT shape starts;'
          '\n      cross-check with a rendered PNG (docx -> pdf -> png) before trusting it.')
    return 0


def cmd_width(args):
    w = text_width(args.text, args.size)
    print('chars=%d  width=%.1f pt  at %.1f pt font' % (len(args.text), w, args.size))
    for cap in (430.0, 491.9, 500.0, 520.0):
        print('   usable %.1f pt -> %d lines' % (cap, line_count(args.text, cap, args.size)))
    return 0


PARA_RE = re.compile(r'(<w:p\b[^>]*>)(<w:pPr>.*?</w:pPr>)(.*?)(</w:p>)', re.S)
RUN_RE = re.compile(r'<w:r>(.*?)</w:r>', re.S)
RPR_RE = re.compile(r'<w:rPr>.*?</w:rPr>', re.S)
BOLD_OFF_RE = re.compile(r'<w:b w:val="(0|false)"')
BOLD_ON_RE = re.compile(r'<w:b\s*/>|<w:b w:val="(1|true)"')


def rpr_templates(para):
    """Reuse the template's own run properties instead of authoring new ones."""
    normal = bold = None
    for run in RUN_RE.findall(para):
        m = RPR_RE.search(run)
        if not m:
            continue
        rpr = m.group(0)
        if normal is None and BOLD_OFF_RE.search(rpr):
            normal = rpr
        if bold is None and BOLD_ON_RE.search(rpr):
            bold = rpr
    return normal, bold


def esc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def cmd_rewrite(args):
    spec = json.load(open(args.spec, encoding='utf-8'))
    match = spec['match']
    tail = spec.get('tail')
    expect = int(spec.get('expect', 2))
    segs = spec['segments']

    names, blobs = read_docx(args.src)
    xml = blobs['word/document.xml'].decode('utf-8')

    target = [b for b in boxes(xml) if match in b['text']]
    if not target:
        print('ABORT: match %r not found in any textbox.' % match)
        print('       The paragraph probably splits the phrase across runs; pick a shorter, safer key.')
        return 2
    uw = target[0]['usable_pt'][0]
    old_txt = target[0]['text']

    state = {'count': 0, 'rpr': (None, None), 'popen': None}

    def repl(m):
        if match not in m.group(0):
            return m.group(0)
        if tail and tail not in m.group(0):
            raise SystemExit('ABORT: paragraph matched %r but not %r' % (match, tail))
        normal, bold = rpr_templates(m.group(0))
        if normal is None or bold is None:
            print('WARN: could not extract both rPr templates; falling back to plain runs.')
            normal, bold = normal or '', bold or normal or ''
        state['rpr'] = (normal, bold)
        state['popen'] = m.group(1)
        state['count'] += 1
        runs = ''.join('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (bold if b else normal, esc(t))
                       for t, b in segs)
        return '%s%s%s</w:p>' % (m.group(1), m.group(2), runs)

    xml2 = PARA_RE.sub(repl, xml)
    if state['count'] != expect:
        print('ABORT: replaced %d paragraph(s), expected %d -- nothing written.'
              % (state['count'], expect))
        return 2
    if match in xml2:
        print('ABORT: old text still present after replace -- nothing written.')
        return 2

    new_txt = ''.join(t for t, _ in segs)
    print('replaced %d paragraph(s)' % state['count'])
    print('  usable width : %.1f pt' % uw)
    print('  old body     : %d chars -> %d lines' % (len(normalize(old_txt)), line_count(normalize(old_txt), uw)))
    print('  new body     : %d chars -> %d lines' % (len(new_txt), line_count(new_txt, uw)))

    blobs['word/document.xml'] = xml2.encode('utf-8')
    write_docx(args.dst, names, blobs)
    print('written: %s' % args.dst)

    # --- verification: nothing else in the document may have changed ----------------
    _, blobs2 = read_docx(args.dst)
    a, b = normalize(all_text(blobs)), normalize(all_text(blobs2))
    print('\ntext delta: %d -> %d chars' % (len(a), len(b)))
    changed = [op for op in difflib.SequenceMatcher(None, a, b).get_opcodes() if op[0] != 'equal']
    print('changed regions: %d (expect %d x runs-of-segments; anything else means you hit the wrong paragraph)'
          % (len(changed), state['count']))
    print('\nNEXT: convert to PDF and render page 1, then LOOK at it:')
    print('  soffice --headless -env:UserInstallation=file:///<scratch>/lo --convert-to pdf --outdir out SRC.docx')
    print('  gswin64c -dNOPAUSE -dBATCH -sDEVICE=png16m -r150 -dFirstPage=1 -dLastPage=1 \\')
    print('           -sOutputFile=out/p1.png out/SRC.pdf')
    print('  "pages == 1" is NOT a pass: overflow in a fixed textbox is silent.')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('probe', help='geometry + capacity of every textbox')
    a.add_argument('src')
    a.set_defaults(fn=cmd_probe)

    b = sub.add_parser('width', help='rendered width / line count of a string')
    b.add_argument('text')
    b.add_argument('--size', type=float, default=9.0)
    b.set_defaults(fn=cmd_width)

    c = sub.add_parser('rewrite', help='replace matching paragraph(s) keeping pPr/rPr')
    c.add_argument('src')
    c.add_argument('dst')
    c.add_argument('spec')
    c.set_defaults(fn=cmd_rewrite)

    ns = p.parse_args(argv)
    return ns.fn(ns)


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Dump a .xlsx to TSV using ONLY the standard library (no openpyxl / pandas).

Usage:
    python xlsx_text_dump.py <file.xlsx> [sheet_index=1] [max_rows=0]

Why this exists: government and campus sites publish their authoritative lists as
xlsx attachments (e.g. a 卫健委 hospital table whose columns are
医疗机构名称 | 级别 | 属性). The sandbox frequently has no pandas, and the naive
regex parse is wrong because **attribute order inside <c> varies between
producers** -- regex-search for t="s" instead of assuming a position.

Handles: sharedStrings (t="s" -> index), inline strings, numbers, empty cells.
"""
import re
import sys
import zipfile


def shared_strings(z):
    try:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
    except KeyError:
        return []
    out = []
    for si in re.findall(r"<si>([\s\S]*?)</si>", ss):
        out.append(re.sub(r"\s+", " ", "".join(re.findall(r"<t[^>]*>([^<]*)</t>", si))).strip())
    return out


def row_values(row_xml, strs):
    vals = []
    for m in re.finditer(r'<c([^>]*)>([\s\S]*?)</c>|<c([^>]*)/>', row_xml):
        attrs = m.group(1) or m.group(3) or ""
        inner = m.group(2) or ""
        t = re.search(r't="(\w+)"', attrs)
        v = re.search(r"<v>([^<]*)</v>", inner)
        isv = re.search(r"<t[^>]*>([^<]*)</t>", inner)
        if t and t.group(1) == "s" and v and v.group(1).isdigit():
            vals.append(strs[int(v.group(1))])
        elif isv:
            vals.append(isv.group(1))
        else:
            vals.append(v.group(1) if v else "")
    return [x for x in vals if x != ""]


def dump(path, sheet_index=1, max_rows=0):
    z = zipfile.ZipFile(path)
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        raise SystemExit("no worksheet found in %s" % path)
    sheet = z.read(sheets[sheet_index - 1]).decode("utf-8", "replace")
    strs = shared_strings(z)
    for i, row in enumerate(re.findall(r"<row[^>]*>([\s\S]*?)</row>", sheet), 1):
        if max_rows and i > max_rows:
            break
        print("\t".join(row_values(row, strs)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    dump(sys.argv[1],
         int(sys.argv[2]) if len(sys.argv) > 2 else 1,
         int(sys.argv[3]) if len(sys.argv) > 3 else 0)

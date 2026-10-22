"""``skills/`` 的元数据索引：扫全部 ``SKILL.md`` 的 frontmatter，**不读正文**。

## 为什么是"索引 + 按需取"（渐进式披露）

本地技能库有一百多篇 ``SKILL.md``，全量正文约 25k 字符、整体几万字符。全量塞进系统提示词
有两个后果：提示词被清单撑满（每轮都付这份 token），而真正要用到的往往只有一篇。
所以这里只建**元数据索引**（名字 + 一句话描述 + 有没有附件目录），
由 ``skill.list`` 按关键词取一页、``skill.load`` 取单篇全文 —— 索引**不进系统提示词**，
``tests/tools/test_skills.py`` 里有一条闸专门钉这件事。

## 三条硬约束（都在下面的代码里有对应位置）

1. **只读元数据**：只读到 frontmatter 的收尾 ``---`` 为止（最多 :data:`HEAD_MAX_CHARS` 字符），
   正文一个字节都不读 —— 扫 130 篇的耗时与内存因此不随正文长度增长；
2. **容错**：坏 frontmatter / 缺 ``name`` / YAML 解析失败 → **跳过这篇并记 warning**，
   一篇坏文件不许把整个索引搞崩（库里 174 篇来路各不相同，这是必然会发生的事）；
3. **去重**：同名 ``SKILL.md`` 只留第一份（按相对路径排序，结果确定），被跳过的那份写进 warning。

缓存键是 ``skills/`` 的绝对路径，**用 mtime 失效**：任一 ``SKILL.md`` 的 ``st_mtime_ns`` 变了，
或 ``skills/`` 根目录本身变了（新增/删除了一篇），下一次取索引就重扫；进程重启自然也是空的。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from scholarscout import paths

log = logging.getLogger(__name__)

#: 每篇 skill 的文件名（固定，不认别的名字）
SKILL_FILE: Final = "SKILL.md"

#: 附件目录：**与 ``SKILL.md`` 同级**（``<skill 目录>/references`` 这样），按目录分类。
#: 顺序即 ``skill.load`` 回包里的键序；新增一类附件只改这里。
ATTACHMENT_DIRS: Final = ("references", "scripts", "templates")

#: frontmatter 的读取上限（字符）。超了说明这篇没有正常收尾，按坏文件跳过 ——
#: 这条上限同时保证"只读元数据"：正文不会被读进来。
HEAD_MAX_CHARS: Final = 8192

#: frontmatter 的形态：文件头 ``---`` 起、``---`` 止（Windows 下是 ``\r\n``，两种都认）
_FRONTMATTER: Final = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

#: 命中的最小连续长度（query 短于它时按 query 长度算）。
#: **为什么要这条下限**：只命中一个字母等于"篇篇都中"—— ``matched`` 会虚高到全库，
#: ``truncated`` 就骗人；只命中两个字母也差不多（``ra`` / ``lo`` 这类双字母到处都是）。
#: 要求三个连续字符（短 query 自然放宽），才对得上"用户打的是个词"。
MIN_HIT: Final = 3

#: 缓存：绝对路径 → 上一次的索引（进程内，一次性扫描 + 复用）
_CACHE: dict[Path, SkillIndex] = {}


@dataclass(frozen=True)
class SkillEntry:
    """索引里的一条：够 ``skill.list`` 用，也够下一轮的 ``/`` 菜单渲染（不需要正文）。"""

    #: frontmatter 的 ``name``（唯一，去重后）
    name: str
    #: frontmatter 的 ``description``（空白归一化；缺失时是空串，仍进索引）
    description: str
    #: 相对 ``skills/`` 的目录，POSIX 形式（``creative/ascii-art``；顶层的就是 ``ascii-art``）
    dir_rel: str
    #: ``SKILL.md`` 的绝对路径（``skill.load`` 读全文用）
    path: Path
    #: 同级附件目录是否存在（**只判存在，不列文件**——列文件是 ``skill.load`` 的事）
    has_references: bool
    has_scripts: bool
    has_templates: bool
    #: 扫描时记下的 mtime，用作缓存失效判据
    mtime_ns: int

    @property
    def display_path(self) -> str:
        """给人看的路径（相对项目根，``skills/`` 开头）：界面上直接显示这一串。"""
        return f"skills/{self.dir_rel}/{SKILL_FILE}"

    def as_summary(self) -> dict[str, Any]:
        """``skill.list`` 的一条：**没有正文**，只有定位与"有没有附件"。"""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "path": self.display_path,
            "has_references": self.has_references,
            "has_scripts": self.has_scripts,
            "has_templates": self.has_templates,
        }

    @property
    def category(self) -> str:
        """按目录分类：取 ``dir_rel`` 的父级（``mlops/inference/llama-cpp`` → ``mlops/inference``）。"""
        parent = self.dir_rel.rpartition("/")[0]
        return parent


@dataclass(frozen=True)
class SkillIndex:
    """一次扫描的结果。``entries`` 已去重且按 ``dir_rel`` 排序（同一棵树每次结果一致）。"""

    root: Path
    entries: tuple[SkillEntry, ...] = ()
    warnings: tuple[str, ...] = ()
    elapsed_ms: float = 0.0
    root_mtime_ns: int = 0
    #: 名字（casefold）→ 条目，去重后的反查表
    _by_name: dict[str, SkillEntry] = field(default_factory=dict, repr=False, compare=False)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def get(self, name: str) -> SkillEntry | None:
        """按名字取一条：先精确，再大小写不敏感（模型常常改大小写）。"""
        exact = next((entry for entry in self.entries if entry.name == name), None)
        if exact is not None:
            return exact
        return self._by_name.get(name.strip().casefold())

    def is_fresh(self) -> bool:
        """缓存是否还有效：目录结构（根 mtime）+ 每篇的 mtime 都没变。

        ``skills/`` 不存在时"空索引"也算新鲜 —— 目录被建出来时根 mtime 会变，那时自然会重扫。
        """
        if not self.root.is_dir():
            return not self.entries
        try:
            if self.root.stat().st_mtime_ns != self.root_mtime_ns:
                return False
            return all(entry.path.stat().st_mtime_ns == entry.mtime_ns for entry in self.entries)
        except OSError:
            return False


def _is_indexable(path: Path, root: Path) -> bool:
    """``.archive`` / ``.hub`` / ``.curator_*`` 这类隐藏目录一律不扫。

    它们是工具自己的记账与归档（``.archive`` 里是 44 篇**已归档**的旧技能），
    扫进来只会让 ``/`` 菜单里出现早就下架的条目。
    """
    return not any(part.startswith(".") for part in path.relative_to(root).parts[:-1])


def _read_metadata(path: Path, warnings: list[str], where: str) -> tuple[str, str] | None:
    """只读到 frontmatter 收尾，返回 ``(name, description)``；坏文件记 warning 并返回 None。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(HEAD_MAX_CHARS)
    except OSError as exc:
        warnings.append(f"{where}：读不了（{exc.__class__.__name__}: {exc}），跳过")
        return None

    matched = _FRONTMATTER.match(head)
    if matched is None:
        warnings.append(f"{where}：坏 frontmatter（缺 --- 或前 {HEAD_MAX_CHARS} 字符没闭合），跳过")
        return None

    try:
        data = yaml.safe_load(matched.group(1))
    except yaml.YAMLError as exc:
        warnings.append(f"{where}：frontmatter 不是合法 YAML（{str(exc).splitlines()[0]}），跳过")
        return None

    if not isinstance(data, dict):
        warnings.append(f"{where}：frontmatter 不是键值映射，跳过")
        return None

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        warnings.append(f"{where}：frontmatter 缺 name（或不是字符串），跳过")
        return None

    description = data.get("description")
    if not isinstance(description, str):
        if description is not None:
            warnings.append(f"{where}：description 不是字符串，按空描述处理")
        description = ""
    return name.strip(), " ".join(description.split())


def scan(root: Path) -> SkillIndex:
    """扫一遍 ``root`` 下所有 ``SKILL.md``，只读 frontmatter，建索引（不做缓存判断）。"""
    started = time.perf_counter()
    entries: list[SkillEntry] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}

    paths_found = (
        sorted(p for p in root.rglob(SKILL_FILE) if _is_indexable(p, root)) if root.is_dir() else []
    )
    for path in paths_found:
        dir_rel = path.parent.relative_to(root).as_posix()
        metadata = _read_metadata(path, warnings, dir_rel)
        if metadata is None:
            continue
        name, description = metadata
        key = name.casefold()
        if key in seen:
            warnings.append(f"{dir_rel}：同名的 {name!r} 已由 {seen[key]} 提供，跳过这一份")
            continue
        seen[key] = dir_rel
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        entries.append(
            SkillEntry(
                name=name,
                description=description,
                dir_rel=dir_rel,
                path=path,
                has_references=(path.parent / "references").is_dir(),
                has_scripts=(path.parent / "scripts").is_dir(),
                has_templates=(path.parent / "templates").is_dir(),
                mtime_ns=mtime_ns,
            )
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    index = SkillIndex(
        root=root,
        entries=tuple(entries),
        warnings=tuple(warnings),
        elapsed_ms=elapsed_ms,
        root_mtime_ns=root.stat().st_mtime_ns if root.is_dir() else 0,
        _by_name={entry.name.casefold(): entry for entry in entries},
    )
    log.info(
        "skills 索引：%d 篇（%.1fms，共 %d 字符描述），跳过 %d 份",
        len(index.entries),
        elapsed_ms,
        sum(len(entry.description) for entry in index.entries),
        len(paths_found) - len(index.entries),
    )
    for warning in index.warnings:
        log.warning("skills 索引：%s", warning)
    return index


def get_index(root: Path | None = None, *, force: bool = False) -> SkillIndex:
    """取索引（有缓存就用缓存，``is_fresh()`` 为假时重扫）。

    ``root`` 缺省是 ``<项目根>/skills``；测试与将来的"换技能库目录"都走这个参数。
    """
    target = (root or paths.skills_dir()).resolve()
    cached = None if force else _CACHE.get(target)
    if cached is not None and cached.is_fresh():
        return cached
    updated = scan(target)
    _CACHE[target] = updated
    return updated


def clear_cache() -> None:
    """只给测试用：丢掉缓存。"""
    _CACHE.clear()


def _longest_hit(query: str, field: str) -> tuple[int, int]:
    """``query`` 在 ``field`` 里**连续命中**的最长片段：返回 ``(长度, 最早位置)``。

    为什么要"连续"而不是"命中几个字符"：字符袋子匹配（query 的字母散落在名字里）几乎篇篇都中，
    排序就退化成按字母序 —— 那不是搜索。取 query 的最长连续子串命中，才对得上"用户打的是哪个词"。
    """
    if not query or not field:
        return 0, 0
    for length in range(len(query), 0, -1):
        for start in range(len(query) - length + 1):
            piece = query[start : start + length]
            at = field.find(piece)
            if at >= 0:
                return length, at
    return 0, 0


def search(index: SkillIndex, query: str, limit: int) -> tuple[list[SkillEntry], int]:
    """字符匹配（大小写不敏感，name 或 description），返回 ``(这一页, 命中总数)``。

    排序：**连续命中越长越靠前** → 命中在 name 里的靠前 → 命中的位置越左越靠前 → 名字字典序。
    最后那条只是为了"同一棵树每次给同一个顺序"，不是语义的一部分。
    """
    text = query.strip().casefold()
    if not text:
        entries = sorted(index.entries, key=lambda entry: entry.name.casefold())
        return entries[:limit], len(entries)

    floor = min(MIN_HIT, len(text))
    ranked: list[tuple[tuple[int, int, int, str], SkillEntry]] = []
    for entry in index.entries:
        name_run, name_at = _longest_hit(text, entry.name.casefold())
        desc_run, desc_at = _longest_hit(text, entry.description.casefold())
        longest = max(name_run, desc_run)
        if longest < floor:
            continue
        # name 命中优先（同一长度时），位置取"真正命中的那个字段"里的位置
        at = name_at if name_run else desc_at
        key = (-longest, 0 if name_run else 1, at, entry.name.casefold())
        ranked.append((key, entry))

    ranked.sort(key=lambda pair: pair[0])
    ordered = [entry for _, entry in ranked]
    return ordered[:limit], len(ordered)


def list_attachments(entry: SkillEntry) -> dict[str, list[dict[str, Any]]]:
    """列 ``references`` / ``scripts`` / ``templates`` 下的文件：**只有相对路径与大小，不读内容**。

    "与 ``SKILL.md`` 同级"是硬要求（用户原话），所以基准是 ``entry.path.parent``；
    空目录/不存在的目录**不出现在回包里**（不编一个空的 ``scripts: []`` 让模型以为有东西）。
    """
    base = entry.path.parent
    groups: dict[str, list[dict[str, Any]]] = {}
    for group in ATTACHMENT_DIRS:
        folder = base / group
        if not folder.is_dir():
            continue
        files: list[dict[str, Any]] = []
        for candidate in sorted(folder.rglob("*")):
            if not candidate.is_file():
                continue
            if any(part.startswith(".") for part in candidate.relative_to(folder).parts):
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            files.append(
                {
                    "path": f"{group}/{candidate.relative_to(folder).as_posix()}",
                    "bytes": size,
                }
            )
        if files:
            groups[group] = files
    return groups

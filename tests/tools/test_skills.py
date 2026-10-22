"""``skills/`` 索引 + ``skill.list`` / ``skill.load``：**按需取，不进系统提示词**。

这一组闸盯四件事，每件都对应一个真实会坏的后果：

1. **索引只读元数据**：正文读进来会让"扫 130 篇"从毫秒级变成几十 MB 的内存占用，
   而且正文里的 ``---`` / ``name:`` 会被误当成 frontmatter（这条有专门的样本）。
2. **坏文件不许拖垮整棵树**：174 篇来路各异的 SKILL.md 里只要有一篇缺收尾 ``---``，
   ``yaml`` 就会抛；异常冒到 ``skill.list`` 就是整层功能不可用。
3. **同名只留一份**：两份同名会让 ``skill.load`` 的结果取决于遍历顺序（不可复现）。
4. **索引不常驻提示词**：这是渐进式披露的全部意义 —— 清单按需取，提示词只留"怎么用"。
   ``test_index_does_not_leak_into_the_system_prompt`` 拿**真实仓库**的 130 条描述逐条比对，
   而不是断言一个手写的假字符串。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scholarscout.rpc.methods import AppContext
from scholarscout.runtime.prompts import build_system_prompt
from scholarscout.skills.index import (
    clear_cache,
    get_index,
    list_attachments,
    scan,
    search,
)
from scholarscout.tools import register_tools
from scholarscout.tools.skills import (
    MAX_LIMIT,
    SPEC_LIST,
    SPEC_LOAD,
    skill_list,
    skill_load,
)

from ..conftest import redirect_home, tool_ctx

#: 真实仓库根（``tests/tools/test_skills.py`` 的上两级）—— 用来钉"真实技能库能被索引"与"没进提示词"
REPO = Path(__file__).resolve().parents[2]

#: 正文样本：故意写一段"看起来像 frontmatter"的内容，证明扫描没把正文当元数据读
BODY = "## 正文\n\nname: 不该被读到\n---\n"


def write_skill(
    root: Path,
    dir_rel: str,
    name: str,
    description: str,
    *,
    body: str = BODY,
    files: tuple[str, ...] = (),
    content: str = "x",
) -> Path:
    """在 ``root/<dir_rel>/SKILL.md`` 造一篇 skill；``files`` 是**与 SKILL.md 同级**的相对附件路径。"""
    folder = root / dir_rel
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\n{body}\n',
        encoding="utf-8",
    )
    for rel in files:
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    """缓存是进程级的：不清会让上一个用例的 tmp 目录跟着进来（虽然键是不同路径，但别赌）。"""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """落盘与技能库根都指到 tmp：工具读 ``<home>/skills``。"""
    return redirect_home(monkeypatch, tmp_path)


@pytest.fixture
def tree(home: Path) -> Path:
    root = home / "skills"
    write_skill(root, "creative/pdf", "pdf", "PDF files: create, read, merge, fill, OCR.")
    write_skill(root, "mlops/local-rag", "local-rag", "Build and troubleshoot local RAG systems.")
    write_skill(root, "productivity/docx", "docx", "Create, read, edit Word .docx files.")
    return root


def test_spec_shape() -> None:
    """两个工具的声明形状：名字带点、不许多余参数、必填项只有 ``skill.load.name``。"""
    assert SPEC_LIST.name == "skill.list" and SPEC_LOAD.name == "skill.load"
    assert SPEC_LIST.parameters["additionalProperties"] is False
    assert SPEC_LOAD.parameters["additionalProperties"] is False
    assert SPEC_LIST.parameters["required"] == []
    assert SPEC_LOAD.parameters["required"] == ["name"]
    assert set(SPEC_LIST.parameters["properties"]) == {"query", "limit"}


def test_scan_reads_metadata_only(tree: Path) -> None:
    """只读 frontmatter：名字/描述对得上，正文里那段假 frontmatter **没被**当成元数据。"""
    index = scan(tree)
    assert index.names == ("pdf", "local-rag", "docx")
    entry = index.get("local-rag")
    assert entry is not None
    assert entry.description == "Build and troubleshoot local RAG systems."
    assert entry.dir_rel == "mlops/local-rag"
    assert entry.category == "mlops"
    assert entry.display_path == "skills/mlops/local-rag/SKILL.md"
    # 正文没被读进索引（描述里不含正文的任何片段）
    assert "不该被读到" not in entry.description
    assert index.warnings == ()


def test_bad_frontmatter_is_skipped_with_warning(home: Path) -> None:
    """缺收尾 ``---`` / YAML 写坏 / 缺 name / frontmatter 超长 —— 四种坏文件都只跳过自己。"""
    root = home / "skills"
    write_skill(root, "good", "good", "A usable skill.")
    (root / "no-closing").mkdir(parents=True)
    (root / "no-closing" / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\n", encoding="utf-8"
    )
    (root / "bad-yaml").mkdir(parents=True)
    (root / "bad-yaml" / "SKILL.md").write_text(
        '---\nname: [unclosed\ndescription: "y"\n---\n\nbody\n', encoding="utf-8"
    )
    (root / "no-name").mkdir(parents=True)
    (root / "no-name" / "SKILL.md").write_text(
        '---\ndescription: "缺名字"\n---\n\nbody\n', encoding="utf-8"
    )
    (root / "huge-frontmatter").mkdir(parents=True)
    (root / "huge-frontmatter" / "SKILL.md").write_text(
        "---\nname: huge\ndescription: " + "x" * 20000 + "\n---\n", encoding="utf-8"
    )

    index = scan(root)

    assert index.names == ("good",)
    assert len(index.warnings) == 4, index.warnings
    joined = "\n".join(index.warnings)
    for where in ("no-closing", "bad-yaml", "no-name", "huge-frontmatter"):
        assert where in joined, f"{where} 的坏文件没有被点名：{index.warnings}"


def test_duplicate_names_keep_one_and_say_which(home: Path) -> None:
    """同名两份：只留第一份（按相对路径排），被跳过的那份**写进 warning**（不静默）。"""
    root = home / "skills"
    write_skill(root, "a/dup", "dup", "First copy.")
    write_skill(root, "b/dup", "dup", "Second copy.")
    index = scan(root)
    assert index.names == ("dup",)
    assert index.entries[0].description == "First copy."
    assert len(index.warnings) == 1
    assert "b/dup" in index.warnings[0] and "跳过" in index.warnings[0]


def test_index_is_cached_and_invalidated_by_mtime(tree: Path) -> None:
    """同一棵树第二次取是**同一个对象**；改过 mtime 之后必须重扫（缓存不能变谎话）。"""
    first = get_index(tree)
    assert get_index(tree) is first

    target = tree / "productivity/docx/SKILL.md"
    target.write_text(
        '---\nname: docx\ndescription: "改成新描述：docx report generation"\n---\n\nbody\n',
        encoding="utf-8",
    )
    stamp = target.stat().st_mtime_ns + 10**9
    os.utime(target, ns=(stamp, stamp))

    second = get_index(tree)
    assert second is not first
    entry = second.get("docx")
    assert entry is not None and entry.description.startswith("改成新描述")


def test_new_skill_directory_invalidates_cache(home: Path) -> None:
    """新增一篇 = 根目录 mtime 变了 → 下次取索引必须看得见它。"""
    root = home / "skills"
    write_skill(root, "one", "one", "第一篇。")
    assert get_index(root).names == ("one",)

    write_skill(root, "two", "two", "第二篇。")
    assert get_index(root).names == ("one", "two")


def test_list_without_query_returns_index_without_body(ctx: AppContext, tree: Path) -> None:
    """无 query：回全量索引（按名字排），每条**只有元数据**，没有正文也没有附件清单。"""
    result = skill_list(tool_ctx(ctx))
    assert result["ok"] is True
    data = result["data"]
    assert data["total_indexed"] == 3
    assert data["matched"] == 3 and data["count"] == 3
    assert data["truncated"] is False
    assert [item["name"] for item in data["skills"]] == ["docx", "local-rag", "pdf"]
    for item in data["skills"]:
        assert set(item) == {
            "name",
            "description",
            "category",
            "path",
            "has_references",
            "has_scripts",
            "has_templates",
        }
        assert "content" not in item
    assert result["refs"]["skill_names"] == ["docx", "local-rag", "pdf"]


def test_list_query_is_case_insensitive(ctx: AppContext, tree: Path) -> None:
    result = skill_list(tool_ctx(ctx), query="LOCAL-RAG")
    assert [item["name"] for item in result["data"]["skills"]] == ["local-rag"]
    assert result["data"]["matched"] == 1


def test_list_limit_is_clamped_and_reports_truncation(ctx: AppContext, tree: Path) -> None:
    """limit 落在 [1, MAX_LIMIT]；回包带**生效值**与 ``truncated``，所以模型看得出被截了。"""
    one = skill_list(tool_ctx(ctx), query="pdf", limit=1)["data"]
    assert one["count"] == 1 and one["matched"] == 1 and one["truncated"] is False

    low = skill_list(tool_ctx(ctx), limit=0)["data"]
    assert low["limit"] == 1 and low["count"] == 1 and low["truncated"] is True

    high = skill_list(tool_ctx(ctx), limit=10**6)["data"]
    assert high["limit"] == MAX_LIMIT
    assert high["count"] == 3 and high["truncated"] is False


def test_ranking_prefers_the_longer_contiguous_hit(ctx: AppContext, home: Path) -> None:
    """连续命中越长越靠前：``docx-report-generation`` 比 ``docx`` 更贴近 "docx report"。"""
    root = home / "skills"
    write_skill(root, "a/docx", "docx", "Word 文件读写。")
    write_skill(root, "b/docx-report", "docx-report-generation", "生成 docx 报告。")
    names = [
        item["name"] for item in skill_list(tool_ctx(ctx), query="docx report")["data"]["skills"]
    ]
    assert names[0] == "docx-report-generation", names


def test_ranking_ties_break_on_hit_position(ctx: AppContext, home: Path) -> None:
    """同样长（都是名字里 3 个字符）：命中位置更靠左的在前 —— ``pdf`` 比 ``nano-pdf`` 近。"""
    root = home / "skills"
    write_skill(root, "a/nano", "nano-pdf", "编辑 PDF 的文字与标题。")
    write_skill(root, "b/pdf", "pdf", "PDF 文件的合并与填表。")
    names = [item["name"] for item in skill_list(tool_ctx(ctx), query="pdf")["data"]["skills"]]
    assert names == ["pdf", "nano-pdf"], names


def test_search_needs_a_contiguous_hit(tree: Path) -> None:
    """字符袋子匹配不算命中：``pfd`` 的字母在 pdf 里都有，但没有三个连续字符对上 → 0 命中。

    否则 ``matched`` 会虚高到全库、``truncated`` 就骗人，排序也退化成按字母序。
    """
    index = scan(tree)
    hits, total = search(index, "pfd", 10)
    assert total == 0 and hits == []

    hits, total = search(index, "rag", 10)
    assert total == 1 and hits[0].name == "local-rag"


def test_load_returns_full_text_and_attachment_manifest(ctx: AppContext, home: Path) -> None:
    """``skill.load`` 回全文 + 附件清单（相对路径 + 字节数，**没有内容**）。"""
    root = home / "skills"
    write_skill(
        root,
        "creative/pdf",
        "pdf",
        "PDF files: create, read, merge.",
        files=("references/api.md", "scripts/run.py", "templates/report.md"),
        content="abcdef",
    )
    result = skill_load(tool_ctx(ctx), "pdf")
    assert result["ok"] is True
    data = result["data"]
    assert data["name"] == "pdf" and data["category"] == "creative"
    assert data["path"] == "skills/creative/pdf/SKILL.md"
    assert data["content"].startswith("---\nname: pdf")
    assert data["chars"] == len(data["content"])
    assert data["attachment_groups"] == ["references", "scripts", "templates"]
    assert data["attachment_count"] == 3 and data["attachment_bytes"] == 18
    assert [item["path"] for item in data["attachments"]["scripts"]] == ["scripts/run.py"]
    for group in data["attachments"].values():
        for item in group:
            assert set(item) == {"path", "bytes"}
    assert result["refs"]["skill_names"] == ["pdf"]


def test_load_omits_attachment_groups_that_are_absent_or_empty(ctx: AppContext, home: Path) -> None:
    """ "has_* 为假就不要编空目录"：没有 ``scripts/``、``templates/`` 只有隐藏文件 → 都不出现。"""
    root = home / "skills"
    write_skill(root, "solo", "solo", "只有 references。", files=("references/a.md",))
    (root / "solo" / "scripts").mkdir()
    (root / "solo" / "templates").mkdir()
    (root / "solo" / "templates" / ".keep").write_text("", encoding="utf-8")

    data = skill_load(tool_ctx(ctx), "solo")["data"]
    assert set(data["attachments"]) == {"references"}
    assert data["attachment_groups"] == ["references"]
    assert data["attachment_count"] == 1


def test_attachments_are_siblings_not_children_of_the_root(home: Path) -> None:
    """附件目录**与 SKILL.md 同级**：隔壁那篇的 ``references/`` 不能算到这一篇头上。"""
    root = home / "skills"
    write_skill(root, "cat/with", "with-refs", "有附件。", files=("references/a.md",))
    write_skill(root, "cat/without", "without-refs", "没附件。")

    index = scan(root)
    with_refs = index.get("with-refs")
    without = index.get("without-refs")
    assert with_refs is not None and without is not None
    assert with_refs.has_references is True
    assert without.has_references is False and without.has_scripts is False
    assert list_attachments(without) == {}


def test_load_unknown_name_is_not_found_envelope(ctx: AppContext, tree: Path) -> None:
    """找不到 = 正常回包（``ok:false`` + ``error_kind=not_found``），并给出可改写的近似名。"""
    result = skill_load(tool_ctx(ctx), "local-ragx")
    assert result["ok"] is False
    assert "没有名为" in result["error"]
    assert result["data"]["error_kind"] == "not_found"
    assert result["data"]["query"] == "local-ragx"
    assert isinstance(result["data"]["suggestions"], list)


def test_load_empty_name_rejects_without_touching_disk(ctx: AppContext) -> None:
    result = skill_load(tool_ctx(ctx), "   ")
    assert result["ok"] is False
    assert result["data"]["error_kind"] == "invalid_argument"


def test_index_does_not_leak_into_the_system_prompt() -> None:
    """**渐进式披露的底线**：真实技能库的每一条描述都不在系统提示词里。

    断言的是**真实仓库**的索引（不是手写样本）：提示词只说明"有这两个工具、什么时候用"，
    清单必须留在工具里按需取。顺带钉住体量 —— 描述总量比整个提示词还大，
    真把清单塞进去，提示词会被撑成两倍多。
    """
    index = get_index(REPO / "skills")
    assert len(index.entries) >= 100, f"真实技能库索引只有 {len(index.entries)} 篇，样本不对"

    prompt = build_system_prompt(auto_artifacts=True)
    assert "skill.list" in prompt and "skill.load" in prompt, "提示词里没有这两个工具的用法"

    description_chars = sum(len(entry.description) for entry in index.entries)
    assert description_chars > 5000, f"描述总量只有 {description_chars} 字符，这条闸就没意义了"
    assert len(prompt) < 8000, f"系统提示词已经 {len(prompt)} 字符，清单被塞进去了？"

    leaked = [entry.name for entry in index.entries if entry.description[:30] in prompt]
    assert leaked == [], f"这些技能的描述进了系统提示词：{leaked[:5]}"
    paths_leaked = [entry.name for entry in index.entries if entry.display_path in prompt]
    assert paths_leaked == [], f"这些技能的路径进了系统提示词：{paths_leaked[:5]}"


def test_real_repo_skills_tree_indexes_cleanly() -> None:
    """仓库自带的技能库要能扫干净：≥100 篇、有附件的那几篇 has_* 认得出、无坏文件。"""
    index = scan(REPO / "skills")
    assert len(index.entries) >= 100
    assert index.warnings == (), f"真实技能库里有坏文件/同名：{index.warnings[:3]}"
    assert all(entry.name and entry.description for entry in index.entries)
    assert any(entry.has_references for entry in index.entries)
    assert any(entry.has_scripts for entry in index.entries)
    assert any(entry.has_templates for entry in index.entries)
    assert not any(
        part.startswith(".") for entry in index.entries for part in entry.dir_rel.split("/")
    )

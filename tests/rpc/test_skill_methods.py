"""`skill.list` / `skill.load` 的 **RPC 包装**契约（界面 `/` 菜单用的那条路）。

这两条 RPC 的全部价值在于"和模型工具一模一样"，所以这里的判据不是手写一份期望字段，
而是**直接与 `tools/skills.py` 的回包逐键比对**：

1. 同一份树、同一个 query，`dispatch("skill.list")` 的回包必须**等于**工具自己的回包 ——
   谁在 RPC 层偷偷改了字段名、补了个"方便前端"的键、或者另写一份筛选，这里立刻红；
2. `skill.load` 的回包同理（`content` / `chars` / `attachment_*` 一个都不许改口径）；
3. 错误按语义分两支：参数写错 → `-32602`；库里没有这个名字 → `-32002`（docs/01 §2），
   且 `error.data.suggestions` 要给近似名（界面上"想打 pdf 结果打成 pdff"要能自己纠）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.skills.index import clear_cache
from scholarscout.tools.skills import skill_list as tool_skill_list
from scholarscout.tools.skills import skill_load as tool_skill_load

from ..conftest import redirect_home, tool_ctx

#: 正文样本：故意写一段"看起来像 frontmatter"的内容，证明回包里的是**全文**而不只是元数据
BODY = (
    "## 正文\n\nname: 不该被读到\n---\n\n先读 `references/cheatsheet.md`，再跑 scripts 下的脚本。"
)


def write_skill(
    root: Path,
    dir_rel: str,
    name: str,
    description: str,
    *,
    files: tuple[str, ...] = (),
) -> Path:
    """在 `root/<dir_rel>/SKILL.md` 造一篇 skill；`files` 是**与 SKILL.md 同级**的相对附件路径。"""
    folder = root / dir_rel
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\n{BODY}\n', encoding="utf-8"
    )
    for rel in files:
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    """索引缓存是进程级的：不清会让上一个用例的 tmp 目录跟着进来。"""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """落盘根与技能库根都指到 tmp（`get_index()` 读 `<home>/skills`），造三篇。"""
    home = redirect_home(monkeypatch, tmp_path)
    root = home / "skills"
    write_skill(
        root,
        "creative/pdf",
        "pdf",
        "PDF files: create, read, merge, fill, OCR, edit text.",
        files=("references/cheatsheet.md", "scripts/merge.py"),
    )
    write_skill(root, "mlops/local-rag", "local-rag", "Build and troubleshoot local RAG systems.")
    write_skill(root, "productivity/docx", "docx", "Create, read, edit Word .docx files.")
    return root


# ------------------------------------------------------------------ skill.list


def test_list_is_the_tool_envelope_verbatim(ctx: AppContext, tree: Path) -> None:
    """`skill.list` 的回包必须**逐键等于**工具自己的回包（含 refs，不含任何额外包装键）。"""
    del tree
    rpc = dispatch(ctx, "skill.list", {})
    tool = tool_skill_list(tool_ctx(ctx))

    assert rpc == tool, "RPC 回包与 tools/skills.py 不一致（菜单与模型会看到两份字段）"
    assert rpc["ok"] is True
    assert set(rpc) == {"ok", "data", "refs"}, f"回包顶层键被改了：{sorted(rpc)}"
    data = rpc["data"]
    assert data["total_indexed"] == 3
    assert data["count"] == 3 and data["matched"] == 3 and data["truncated"] is False
    assert data["limit"] == 20, "默认 limit 不是工具的 DEFAULT_LIMIT"
    assert [item["name"] for item in data["skills"]] == ["docx", "local-rag", "pdf"], (
        "无 query 时按名字排序（与工具的 search 同一口径）"
    )
    # **不回正文**：一条都不能带 content / chars，否则菜单会把 130 篇正文拉进前端
    assert all("content" not in item and "chars" not in item for item in data["skills"])
    assert data["skills"][2]["has_references"] is True
    assert data["skills"][2]["has_scripts"] is True
    assert data["skills"][0]["has_templates"] is False


def test_list_query_and_limit_match_the_tool(ctx: AppContext, tree: Path) -> None:
    """过滤与排序只在 `skills/index.search` 里定义：RPC 传 query 的结果必须与工具一致。"""
    del tree
    rpc = dispatch(ctx, "skill.list", {"query": "rag", "limit": 1})
    tool = tool_skill_list(tool_ctx(ctx), "rag", 1)

    assert rpc == tool
    names = [item["name"] for item in rpc["data"]["skills"]]
    assert names == ["local-rag"], f"连续命中排在前面那条规则变了：{names}"
    assert rpc["data"]["truncated"] is False, "命中数等于本页条数时不算截断"
    assert rpc["data"]["limit"] == 1, "limit 必须回**生效值**"


def test_list_rejects_a_non_string_query(ctx: AppContext, tree: Path) -> None:
    """`query` 类型不对 → `-32602`（不静默当空串：那会回一整页，看着像"没过滤"）。"""
    del tree
    with pytest.raises(RpcError) as caught:
        dispatch(ctx, "skill.list", {"query": 7})
    assert caught.value.code == int(Code.INVALID_PARAMS)


# ------------------------------------------------------------------ skill.load


def test_load_returns_full_text_and_attachment_groups(ctx: AppContext, tree: Path) -> None:
    """`skill.load` 回信封 = 工具回包：全文、字数、附件清单（只有路径与字节数）。"""
    del tree
    rpc = dispatch(ctx, "skill.load", {"name": "pdf"})
    tool = tool_skill_load(tool_ctx(ctx), "pdf")

    assert rpc == tool, "RPC 回包与 tools/skills.py 不一致"
    data = rpc["data"]
    assert data["name"] == "pdf"
    assert data["chars"] == len(data["content"]) > 0
    assert "先读 `references/cheatsheet.md`" in data["content"], "回包里不是 SKILL.md 全文"
    assert data["attachment_groups"] == ["references", "scripts"]
    assert data["attachment_count"] == 2
    assert [item["path"] for item in data["attachments"]["references"]] == [
        "references/cheatsheet.md"
    ]
    assert "templates" not in data["attachments"], "不存在的附件目录不许编一个空数组"
    assert rpc["refs"] == {"skill_names": ["pdf"]}


def test_load_name_is_case_insensitive_like_the_tool(ctx: AppContext, tree: Path) -> None:
    """大小写不敏感取（`skills/index.get` 的既有行为）—— 界面上的名字可能被人手改过大小写。"""
    del tree
    rpc = dispatch(ctx, "skill.load", {"name": "Local-RAG"})
    assert rpc["data"]["name"] == "local-rag"


def test_load_unknown_name_is_not_found_with_suggestions(ctx: AppContext, tree: Path) -> None:
    """库里没有这个名字 → `-32002`（资源不存在），`data` 里带 `error_kind` 与近似名。"""
    del tree
    with pytest.raises(RpcError) as caught:
        dispatch(ctx, "skill.load", {"name": "pdff"})
    error = caught.value
    assert error.code == int(Code.NOT_FOUND), "查不到不是参数错误，是资源不存在（docs/01 §2）"
    data = error.data
    assert data["error_kind"] == "not_found"
    assert data["query"] == "pdff"
    assert "pdf" in data["suggestions"], f"近似名没给出来：{data.get('suggestions')}"


def test_load_requires_a_name(ctx: AppContext, tree: Path) -> None:
    """缺 name / 空串 → `-32602`（不默认取第一篇：那会把"没说清"显示成"取到了"）。"""
    del tree
    for params in ({}, {"name": ""}, {"name": "   "}, {"name": 3}):
        with pytest.raises(RpcError) as caught:
            dispatch(ctx, "skill.load", params)
        assert caught.value.code == int(Code.INVALID_PARAMS), f"{params} 没被判成参数错误"

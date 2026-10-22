"""设置里「安全与审批」（py.exec 永久授权）的入库契约：分组位置 / 空态文案 / 撤销失败分支。

为什么要有这条测试：这套东西此前只有 `run/ui_sandbox_rules_probe.py` 一个探针 —— 而 `run/` 在
gitignore 里，探针**不入库**。也就是说改坏这里，CI 上不会有任何东西变红。这里把三条最容易
悄悄退化的契约钉在源码文本上（真渲染由探针在无头 Edge 里量，探针失败时先看它）：

1. **分组**：这一组挂在「模型与生成」末尾（`renderSandboxRules(panels.model)`），
   而且**没有**新增第六个设置分类 —— 五分类是几何基线，多一格整块面板尺寸就动；
2. **空态**：没有任何永久授权时给一句完整解释，不是留一个空框；
3. **撤销失败**：只有后端确认 `removed === true` 才移除那一行；失败时行**仍在**、
   红字说明「仍然有效」、按钮可再点，且**绝不**提示「已撤销」。

第 3 条为什么要按**顺序**断言：老写法常见的是先 `removeChild` 再报错，界面表现为
「行没了 + 报错」—— 用户以为撤掉了，实际下一次还会静默放行。文本里这三句都在时，
只有顺序能区分「失败保留」和「失败也已移除」。
"""

from __future__ import annotations

from ._ui import APP_JS, UI_DIR

PROFILE_JS = (UI_DIR / "profile.js").read_text(encoding="utf-8")

SANDBOX_HEAD = "async function renderSandboxRules"
REVOKE_HEAD = "async function revokeSandboxRule"
OPEN_SETTINGS = "async function openSettings"
EMPTY_COPY = "暂无永久授权：模型每次执行代码前都会先问你；你选「永久允许」后才会出现在这里。"


def _block(start: str, end: str) -> str:
    """从 start 锚点到**其后**第一次出现的 end 锚点（反序就切到文件尾，不返回空串）。"""
    at = PROFILE_JS.index(start)
    try:
        return PROFILE_JS[at : PROFILE_JS.index(end, at)]
    except ValueError:
        return PROFILE_JS[at:]


def test_group_is_appended_to_model_category() -> None:
    """分组挂在「模型与生成」末尾，且在 `renderAppearance`（最后一个分类）之前被调用。"""
    open_settings = _block(OPEN_SETTINGS, "function closeSettings")
    assert "await renderSandboxRules(panels.model)" in open_settings, (
        "openSettings 没调 renderSandboxRules(panels.model)，这一组根本不会画出来"
    )
    assert open_settings.index("renderSandboxRules(panels.model)") < open_settings.index(
        "renderAppearance(panels.appearance)"
    ), "「安全与审批」应当在「模型与生成」里画完再画外观"


def test_no_sixth_settings_category_was_added() -> None:
    """五分类不能被这条功能撑成六格：多一格就动了面板几何基线（920×620）。"""
    at = APP_JS.index("var SETTINGS_CATEGORIES = [")
    spec = APP_JS[at : APP_JS.index("var PROFILE_CATEGORIES = [", at)]
    for name in ("model", "channel", "search", "data", "appearance"):
        assert f'name: "{name}"' in spec, f"设置分类少了「{name}」"
    assert spec.count('name: "') == 5, "设置分类不是 5 个了，几何基线会跟着动"


def test_group_heading_and_hint_say_what_it_is() -> None:
    group = _block(SANDBOX_HEAD, "if (session.length)")
    assert '"安全与审批"' in group, "分组标题不是「安全与审批」"
    assert "py.exec" in group, "说明里没点出 py.exec，用户不知道这是哪类授权的列表"


def test_empty_state_copy_is_complete() -> None:
    """空态必须有那句完整解释（只留空白会让人以为这功能没加载）。"""
    assert EMPTY_COPY in PROFILE_JS, "空态文案被改掉了或不见了"
    empty_fn = _block("function sandboxEmptyNote", "function sandboxRuleRow")
    assert "暂无永久授权" in empty_fn, "空态文案不在 sandboxEmptyNote 里"
    # 没有永久授权时走空态分支，而不是什么都不画
    assert "listBox.appendChild(sandboxEmptyNote())" in _block(SANDBOX_HEAD, "if (session.length)")


def test_revoke_failure_keeps_row_and_says_still_valid() -> None:
    """失败分支：行留着（removeChild 在 `removed !== true` 之后）+ 红字「仍然有效」。"""
    body = _block(REVOKE_HEAD, "async function renderSandboxRules")
    assert "仍然有效" in body, "撤销失败时没告诉用户这条授权还在生效"
    assert "button.disabled = false" in body, "失败后按钮没放开，用户点第二次没反应"

    removed_check = body.index("result.removed !== true")
    remove_row = body.index("box.removeChild(row)")
    assert removed_check < remove_row, (
        "先移除了行、后才判断 removed —— 失败时行也会消失，用户以为撤销成功"
    )
    # 「已撤销」这句只能在确认移除之后出现
    assert body.index("已撤销") > remove_row, "还没移除就提示「已撤销」了"


def test_revoke_failure_message_does_not_claim_success() -> None:
    """catch 分支里不许出现「已撤销 / 撤销成功」，只能报失败原因。"""
    body = _block(REVOKE_HEAD, "async function renderSandboxRules")
    catch_at = body.index("} catch (error) {")
    catch_block = body[catch_at : body.index("if (!result || result.removed !== true)")]
    assert "撤销失败" in catch_block
    assert "已撤销" not in catch_block, "请求报错时还提示「已撤销」"


def test_session_rules_are_split_into_their_own_group() -> None:
    """`session` 级（关掉应用就没）不能混进「永久授权」那组，否则用户以为它落了盘。"""
    group = _block(SANDBOX_HEAD, "async function openSettings")
    assert 'rule.level !== "session"' in group, "永久组没有排除 session 级规则"
    assert '"本会话临时授权"' in group, "session 级规则没有单独的分组标题"

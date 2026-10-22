# Streamlit 固定底部输入栏

## 场景

多轮对话中，输入框需要固定在屏幕底部，不随消息滚动而移动。侧栏可收展时，表单左边距需同步变化。

## 错误方案（翻车史 2026-07-07）

用 HTML div 包裹 Streamlit 组件：

```python
st.markdown('<div class="chat-input-fixed">', unsafe_allow_html=True)
with st.form("chat_form", border=False):
    ...
st.markdown('</div>', unsafe_allow_html=True)
```

**行不通。** Streamlit 将每个 `st.markdown()` 渲染为独立的 `stMarkdown` DOM 块，开闭 `<div>` 跨两个不连通的父容器。浏览器不会跨 DOM 树配对标签。

## 正确方案

CSS 直接定位 `[data-testid="stForm"]` 元素。`position: fixed` 使元素脱离文档流，无论 DOM 层级如何都能固定在视窗底部。

⚠️ **Streamlit 版本差异**：`st.form()` 在不同版本渲染为不同 HTML 标签：
- Streamlit ≤ 1.34 → `<form data-testid="stForm">`
- Streamlit ≥ 1.35 → `<div data-testid="stForm">`

**CSS 选择器必须用 `[data-testid="stForm"]`（不加 `form` 前缀）才能兼容两版。** 用 `form[...]` 在新版 Streamlit 下匹配不到任何元素，`position: fixed` 不会生效且无明显报错。

### 1. 基础 CSS

```css
/* 主内容下留空间，不被固定表单遮盖 */
.main .block-container {
    padding-bottom: 80px !important;
}
/* 表单脱离文档流固定在底部 — 用 data-testid 属性选择器兼容新旧 Streamlit */
[data-testid="stForm"] {
    position: fixed !important;
    bottom: 0;
    right: 0;
    z-index: 100;
    background: #F9F6F0;
    padding: 4px 16px 12px;
    border-top: 1px solid #E8DED4;
    margin-bottom: 0 !important;
}
```

### 2. 动态侧栏配合

表单的 `left` 值必须匹配侧栏当前宽度。侧栏宽度在动态 CSS 块中根据 `st.session_state.sidebar_collapsed` 设置。在同一个动态块里追加表单 left：

```python
if not st.session_state.show_search_modal:
    sw = "60px" if st.session_state.sidebar_collapsed else "280px"
    st.markdown(f"""
    <style>
    section[data-testid="stSidebar"] {{
        min-width:{sw} !important; max-width:{sw} !important;
        transition:all .2s ease;
    }}
    [data-testid="stForm"] {{
        left:{sw} !important;
    }}
    </style>
    """, unsafe_allow_html=True)
```

### 3. 表单代码不变

```python
with st.form("chat_form", border=False):
    c1, c2, c3 = st.columns([4, 1, 1])
    with c1:
        query = st.text_input("", ..., key="chat_query")
    with c2:
        submitted = st.form_submit_button("↑", type="primary")
    with c3:
        source_filter = st.selectbox("", ..., key="chat_source_filter")
```

不要加任何手动的 HTML wrapper div。

## 坑

- `position: fixed` 脱离正常流，需要 `z-index > 0` 避免被其他元素遮住
- `.main .block-container` 必须加 `padding-bottom`，否则消息末尾会被表单挡住
- 侧栏宽度变化时 `left` 值必须在**同一个**动态 CSS 块中同步更新（两个独立的 `st.markdown` CSS 块会有优先级竞争）
- 表单的 `margin-bottom: 0 !important` 可能有 Streamlit 默认下边距需要覆盖
- **Streamlit 版本差异（翻车史 2026-07）**：`st.form()` 在不同 Streamlit 版本渲染为不同 HTML 元素。新版（≥1.35）渲染为 `<div data-testid="stForm">`，旧版渲染为 `<form data-testid="stForm">`。CSS 选择器**必须用属性选择器 `[data-testid="stForm"]`** 而非 `form[data-testid="stForm"]`，否则新版下选择器不匹配，`position: fixed` 不生效且无报错。调试方法：在浏览器 Console 运行 `getComputedStyle(document.querySelector('[data-testid="stForm"]')).position`，应返回 `"fixed"`，若返回 `"static"` 则选择器有误。

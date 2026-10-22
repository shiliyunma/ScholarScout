# Streamlit 图标按钮模式：st.button + CSS background-image

当需要将 PNG 图标（而非 emoji）作为按钮内容时，**不要用 `<a>` tag + query params**（点击不可靠）。

## 正确做法

```python
# 1. 工具函数：缩放大图 → base64 data URL（@cache_data 缓存）
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image

@st.cache_data
def _icon_data_url(name: str, size: int = 24) -> str:
    """返回 PNG 图标的 data: URL（用于 CSS background-image）"""
    path = Path(f"app/resources/{name}")
    if not path.exists():
        return ""
    img = Image.open(path)
    ratio = size / img.width
    new_h = int(img.height * ratio)
    img = img.resize((size, new_h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


# 2. 注入 CSS（用 button[title="xxx"] 通过 help 参数匹配）
search_icon = _icon_data_url("搜索.png")

st.markdown(f"""
<style>
button[title="搜索日志"] {{
    background: transparent url('{search_icon}') no-repeat center !important;
    background-size: 24px !important;
    width: 36px !important;
    height: 36px !important;
    min-width: 36px !important;
    border: none !important;
    box-shadow: none !important;
    font-size: 0 !important;
    color: transparent !important;
}}
</style>
""", unsafe_allow_html=True)


# 3. 按钮自身用空格 label，help 成为 HTML title
if st.button(" ", key="search_btn", help="搜索日志"):
    st.session_state.show_search_modal = True
    st.rerun()
```

## 原理解释

| 组件 | 作用 |
|------|------|
| `st.button(" ", help="搜索日志")` | 创建空白按钮，`help` 参数变成 HTML `<button title="搜索日志">` |
| CSS `button[title="搜索日志"]` | 通过 `title` 属性精确匹配按钮，设置背景图 |
| `font-size: 0; color: transparent` | 隐藏 label 空格文本 |
| `background-size: 24px` | 控制图标显示尺寸 |
| PIL resize + cache_data | 1440x1343 大图（600KB）缩至 24px 再 base64，仅计算一次 |

## 坑

- 原始 PNG 很大（1440x1343，~600KB），**必须用 PIL 缩放后再 base64**，否则 HTML 中有 800KB 冗余 base64 数据
- 不同按钮如果有相同 `help` 文字，CSS 规则会同时匹配——这对相同功能的按钮（如收起/展开态的搜索按钮）是有意的
- `<a href="?search_modal=1">` + `st.query_params` 方式不可靠：浏览器 URL 导航与 Streamlit SPA 机制有时序冲突，点击后页面刷新但动作不触发

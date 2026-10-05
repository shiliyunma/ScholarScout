"""UI 资源校验契约：文件齐全、方法名真实存在、离线无外链。"""

from __future__ import annotations

from pathlib import Path

from scholarscout.desktop import assets


def _write(base: Path, name: str, text: str) -> None:
    (base / name).write_text(text, encoding="utf-8")


def _minimal_ui(base: Path, *, js: str | None = None, html: str | None = None) -> None:
    base.mkdir(parents=True, exist_ok=True)
    _write(
        base,
        "index.html",
        html
        or '<html><head><link rel="stylesheet" href="style.css"></head>'
        '<body><script src="app.js"></script></body></html>',
    )
    _write(
        base,
        "app.js",
        js if js is not None else 'var s={call:function(m){} };s.call("system.health", {});',
    )
    _write(base, "style.css", "body{margin:0}")


def test_real_ui_passes() -> None:
    assert assets.verify_ui() == []


def test_called_methods_extracts_literal_names() -> None:
    text = 'scout.call("archive.list", {}); scout.call( "route.get" , {a:1}); scout.call(name);'
    assert assets.called_methods(text) == {"archive.list", "route.get"}


def test_missing_files_reported(tmp_path: Path) -> None:
    problems = assets.verify_ui(tmp_path)
    assert any("缺少 UI 文件：index.html" in problem for problem in problems)
    assert any("app.js" in problem for problem in problems)


def test_missing_reference_reported(tmp_path: Path) -> None:
    _minimal_ui(tmp_path, html="<html><body>没有脚本也没有样式</body></html>")
    problems = assets.verify_ui(tmp_path)
    assert any("没有引入 app.js" in problem for problem in problems)
    assert any("没有引入 style.css" in problem for problem in problems)


def test_external_url_reported(tmp_path: Path) -> None:
    _minimal_ui(
        tmp_path, html='<html><link rel="stylesheet" href="https://cdn.example.com/x.css"></html>'
    )
    assert any("外网资源" in problem for problem in assets.verify_ui(tmp_path))


def test_unknown_method_reported(tmp_path: Path) -> None:
    _minimal_ui(tmp_path, js='scout.call("archive.nope", {});')
    problems = assets.verify_ui(tmp_path)
    assert any("不存在的方法：archive.nope" in problem for problem in problems)


def test_no_calls_reported(tmp_path: Path) -> None:
    _minimal_ui(tmp_path, js="var a = 1;")
    assert any("没调用任何后端方法" in problem for problem in assets.verify_ui(tmp_path))


def test_extra_entry_page_reported(tmp_path: Path) -> None:
    _minimal_ui(tmp_path)
    _write(tmp_path, "debug.html", "<html></html>")
    assert any("多余的入口页：debug.html" in problem for problem in assets.verify_ui(tmp_path))


def test_main_prints_ok(capsys) -> None:
    assert assets.main() == 0
    assert "[OK]" in capsys.readouterr().out


def test_ui_references_every_panel_target() -> None:
    """界面上的按钮/面板必须真的有对应节点（防止改 HTML 忘了改 JS）。"""
    html = (assets.ui_dir() / "index.html").read_text(encoding="utf-8")
    for anchor in (
        'id="status"',
        'id="btn-theme"',
        'id="session-list"',
        'id="messages"',
        'id="composer"',
        'id="source-list"',
        'id="route-list"',
        'id="card-list"',
    ):
        assert anchor in html

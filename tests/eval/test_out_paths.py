"""`--out` 的语义：以 `.json` 结尾＝**文件路径**，否则＝**目录/前缀**。

踩过的真 bug：`--out eval/reports/20261024-130257-e4dialogues.json` 被当成目录，
脚本在 `.json` 后面又建了一个同名目录，报告落成
`...-e4dialogues.json/20261024-130258-e4dialogues.json` —— 预期路径上没有文件，
外部脚本读不到。这里把两种语义都钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

from eval import report, run_dialogues


def test_json_out_resolves_to_that_file(tmp_path: Path) -> None:
    """`.json` 结尾 → 就写到这个文件，`.md` 是同名换后缀。"""
    target = tmp_path / "nested" / "deeper" / "20261024-130257-e4dialogues.json"

    json_path, md_path = report.resolve_report_paths(target, "unused-stem")

    assert json_path == target, "以 .json 结尾的 --out 必须原样当作文件路径"
    assert md_path == target.with_suffix(".md"), ".md 要与 .json 同名（换后缀）"
    assert json_path.parent.is_dir(), "父目录不存在时要自动创建"


def test_non_json_out_keeps_directory_semantics(tmp_path: Path) -> None:
    """不带 `.json` 后缀 → `<out>/<stem>.{json,md}`（既有用法不变）。"""
    out = tmp_path / "reports"

    json_path, md_path = report.resolve_report_paths(out, "20261024-130257-e4dialogues")

    assert json_path == out / "20261024-130257-e4dialogues.json"
    assert md_path == out / "20261024-130257-e4dialogues.md"
    assert out.is_dir(), "目录模式下要建出这个目录"


def test_dialogues_json_out_writes_file_not_directory(tmp_path: Path) -> None:
    """端到端：`--out xxx.json` 之后，路径上必须是**文件**，且旁边有 .md。"""
    target = tmp_path / "reports" / "20261024-130257-e4dialogues.json"

    assert run_dialogues.main(["--plan", "--out", str(target)]) == 0

    assert target.is_file(), "报告必须落在 --out 指定的那个文件上"
    assert not target.is_dir(), "不许在 .json 后面再建同名目录（这正是那个 bug）"
    assert target.with_suffix(".md").is_file(), "同时要写同名 .md"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mode"] == "plan"
    assert sorted(item.name for item in target.parent.iterdir()) == [
        "20261024-130257-e4dialogues.json",
        "20261024-130257-e4dialogues.md",
    ], "同目录下只该有这两份产物，不该多出目录或重复副本"


def test_dialogues_dir_out_still_stamps_files(tmp_path: Path) -> None:
    """端到端：不带后缀时行为不变 —— `<out>/<时间戳>-e4dialogues.{json,md}`。"""
    out = tmp_path / "reports"

    assert run_dialogues.main(["--plan", "--out", str(out)]) == 0

    jsons = sorted(out.glob("*-e4dialogues.json"))
    mds = sorted(out.glob("*-e4dialogues.md"))
    assert len(jsons) == 1, f"目录模式下应按时间戳落一份 json，实际 {jsons}"
    assert len(mds) == 1, f"目录模式下应按时间戳落一份 md，实际 {mds}"
    assert jsons[0].stem == mds[0].stem
    assert out.is_dir()


def test_write_reports_returns_the_paths_it_wrote(tmp_path: Path) -> None:
    """`_write_reports` 返回的路径必须就是实际写出的文件（打印/外部脚本都靠它）。"""
    res = {"mode": "plan", "dialogues": [], "total_turns": 0, "arg_problems": []}

    json_path, md_path = run_dialogues._write_reports(res, tmp_path / "a.json", "20261024-130257")

    assert json_path.is_file() and md_path.is_file()
    assert json_path.name == "a.json" and md_path.name == "a.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["mode"] == "plan"
    assert md_path.read_text(encoding="utf-8").startswith("# E4 六对话过程评分")

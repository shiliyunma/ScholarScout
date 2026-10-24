"""评测用隔离的数据目录：临时根 + 密钥仍可读（不污染 ``data/raw`` 与 ``data/text``）。

踩过的真缺陷：`eval/run_dialogues.py --live` 本来就用**临时库**（`db_mode: temporary`），
但 `source.fetch` 的原件/全文是按 `paths.home()` 落盘的 —— 跑完库销毁、文件留下成孤儿。
实测一次 E4 全跑在用户的真实 `data/raw/` 里留了 43 个文件（`E4-c1..c6` 六个对话的产物，
日期横跨 10-11 / 10-12 / 10-24）。本文件把「换根」的每条承诺都钉住：

1. 评测路径下 `data/raw`、`data/text` 落在临时根，**不在**仓库 `data/`；
2. 跑完临时根被删（默认）；
3. `--keep-data` 时保留；
4. 换根之后 `.env` 里的密钥**仍读得到**；
5. 正常（非评测）路径不变。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from eval import run_dialogues
from scholarscout import config as config_mod
from scholarscout import paths
from scholarscout.config import Config


def _raw_under(root: Path, sha: str = "ab" * 32) -> Path:
    return paths.raw_path_for_layout(
        "session/format",
        session="E4-c1-隔离测试",
        fmt="paper",
        collected="2026-10",
        published="2026-10",
        title="隔离测试文档",
        sha256=sha,
        ext=".html",
        root=root,
    )


def test_eval_paths_land_in_temp_root_not_repo_data() -> None:
    """① 临时根下 `data/raw`、`data/text` 与仓库 `data/` 是两套。"""
    repo_data = paths.data_dir()

    with paths.isolated_home(prefix="scs-eval-") as root:
        assert paths.home() == root
        assert paths.data_dir() == root / "data"
        assert paths.data_dir() != repo_data

        raw = _raw_under(root)
        assert (root / "data" / "raw") in raw.parents, "原件必须落在临时根的 data/raw 下"
        assert repo_data not in raw.parents, "原件不许落在仓库 data/ 下"

        text = paths.text_path_for_layout(
            "session/format",
            session="E4-c1-隔离测试",
            fmt="paper",
            title="隔离测试文档",
            sha256="ab" * 32,
            root=root,
        )
        assert (root / "data" / "text") in text.parents
        assert repo_data not in text.parents

        # 落盘根换过之后，`rel_to_home` / `resolve_rel` 仍是一对（库里的相对路径能解回来）
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("x", encoding="utf-8")
        rel = paths.rel_to_home(raw, root)
        assert rel.startswith("data/raw/")
        assert paths.resolve_rel(rel, root) == raw


def test_temp_root_is_deleted_by_default() -> None:
    """② 退出上下文即删（默认），`home()` 复原。"""
    before = paths.home()
    with paths.isolated_home(prefix="scs-eval-") as root:
        probe = root / "data" / "raw" / "probe.txt"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("孤儿", encoding="utf-8")

    assert not root.exists(), "默认必须删掉临时根（否则就是换了个地方留孤儿）"
    assert paths.home() == before, "退出后 home() 必须复原，不能把进程带偏"


def test_keep_data_preserves_temp_root() -> None:
    """③ `keep=True`（`--keep-data`）时保留，供排查。"""
    with paths.isolated_home(prefix="scs-eval-", keep=True) as root:
        probe = root / "data" / "raw" / "probe.txt"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("留证据", encoding="utf-8")

    try:
        assert root.is_dir(), "keep=True 时临时根必须还在"
        assert probe.read_text(encoding="utf-8") == "留证据"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_env_keys_survive_home_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """④ 换根不影响密钥：`.env` 的键在 `os.environ` 里，换根只动路径解析。"""
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    (fake_home / ".env").write_text("LLM_API_KEY=sk-fake-isolation-1234\n", encoding="utf-8")
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(fake_home))
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    # 评测的做法：**先**读配置与密钥（load_config 内部 load_dotenv 灌进 os.environ）
    cfg = config_mod.load_config(fake_home / "config.yaml")
    assert cfg.chat.api_key() == "sk-fake-isolation-1234", "换根之前必须读得到密钥"

    with paths.isolated_home(prefix="scs-eval-") as root:
        assert paths.home() == root, "进程内覆盖优先于 SCHOLARSCOUT_HOME 环境变量"
        assert not (root / ".env").exists(), "临时根里没有 .env —— 密钥不可能来自它"
        # 关键断言：换根之后密钥仍读得到（走进程环境，不走临时根）
        assert cfg.chat.api_key() == "sk-fake-isolation-1234"
        assert config_mod.load_dotenv(root / ".env") == {}


def test_normal_home_is_unchanged_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑤ 回归：没有覆盖时 `home()` 仍是源码根，`data/` 仍在仓库下。"""
    monkeypatch.delenv("SCHOLARSCOUT_HOME", raising=False)
    paths.override_home(None)

    source_root = Path(paths.__file__).resolve().parents[2]
    assert paths.home() == source_root, "正常路径必须还是包目录的上三级"
    assert paths.data_dir() == source_root / "data"
    assert paths.run_dir() == source_root / "run"

    with paths.isolated_home(prefix="scs-eval-"):
        assert paths.home() != source_root
    assert paths.home() == source_root, "退出后必须回到原值"


def _fake_live(calls: list[Path], *, write: bool = True) -> Callable[..., dict[str, Any]]:
    """替身 `run_live`：像真的一样往 `paths.data_dir()/raw` 落一个文件。"""

    def run(ctx: Any, rows: list[dict[str, Any]], *, limit: int | None = None) -> dict[str, Any]:
        calls.append(paths.home())
        if write:
            probe = paths.data_dir() / "raw" / "session-x" / "probe.html"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("评测产物", encoding="utf-8")
        return {"mode": "live", "dialogues": []}

    return run


def _stub_load_config(config: Config) -> Callable[..., Config]:
    """替身 `load_config`：不读仓库 `config.yaml` / `.env`，免得把真密钥灌进测试进程。"""

    def load(config_path: Path | None = None, *, load_env: bool = True) -> Config:
        return replace(config, data_dir=paths.data_dir(), db_path=paths.db_path())

    return load


def test_eval_main_writes_into_temp_root_and_cleans_up(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端（不跑模型）：`--live` 的产物落临时根，跑完根被删，报告照写。"""
    repo_data = paths.data_dir()
    roots: list[Path] = []
    monkeypatch.setattr(config_mod, "load_config", _stub_load_config(config))
    monkeypatch.setattr(run_dialogues, "run_live", _fake_live(roots))

    out = tmp_path / "reports" / "e4.json"
    assert run_dialogues.main(["--live", "--limit", "1", "--out", str(out)]) == 0

    assert roots, "run_live 应被调用"
    assert roots[0] != repo_data.parent, "评测的数据根不许是仓库根"
    assert not roots[0].exists(), "默认跑完就删临时根"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["db_mode"].startswith("temporary")
    assert "data_root" not in payload, "默认不留路径（根已经没了）"


def test_eval_main_keep_data_flag_keeps_root(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--keep-data`：根留着，路径写进报告，方便排查。"""
    roots: list[Path] = []
    monkeypatch.setattr(config_mod, "load_config", _stub_load_config(config))
    monkeypatch.setattr(run_dialogues, "run_live", _fake_live(roots))

    out = tmp_path / "reports" / "e4-keep.json"
    assert run_dialogues.main(["--live", "--keep-data", "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    kept = Path(payload["data_root"])
    try:
        assert kept == roots[0]
        assert kept.is_dir(), "--keep-data 时临时根必须保留"
        assert (kept / "data" / "raw" / "session-x" / "probe.html").is_file()
        assert Path(payload["db_path"]).exists(), "临时库也留着（同源的另一半）"
    finally:
        shutil.rmtree(kept, ignore_errors=True)

"""scripts/run_daily.py 的命令行测试：全程离线（--dry-run + 临时项目根），验收退出码与产物。

覆盖：``--dry-run --limit`` 真的建出 web/index.html 并返回 0、``--json`` 输出可解析摘要、
``--roles`` / ``--platforms`` / ``--no-tts`` 透传给 ``pipeline.run``、失败项 → 退出码 1、
配置/参数问题 → 退出码 2、``--limit 0`` 与越界门槛走 argparse 用法错误、
以及子进程跑一遍真实命令（顺带验证没 ``pip install`` 时也能靠 sys.path 兜底起来）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from foundry.config import ENV_ROOT
from foundry.pipeline import STAGES

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_daily.py"
REPO_CONFIG = REPO_ROOT / "config"
SOURCES_YAML = """
defaults:
  timeout: 5
sources:
  - {id: one, name: One News, category: news, type: rss, enabled: true, url: "https://example.com/one.rss"}
"""


def load_script():
    spec = importlib.util.spec_from_file_location("run_daily", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_daily = load_script()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """临时项目根 + 让 load_settings() 认它（等价于 ``FOUNDRY_ROOT=... python scripts/run_daily.py``）。"""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "foundry"\n', encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    shutil.copyfile(REPO_CONFIG / "roles.yaml", config / "roles.yaml")
    shutil.copyfile(REPO_CONFIG / "platforms.yaml", config / "platforms.yaml")
    (config / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    return tmp_path


def fake_summary(**overrides) -> dict:
    payload = {
        "run_at": "2026-10-27T12:00:00+00:00",
        "mode": "dry-run",
        "sources": 1,
        "articles": 2,
        "stories": 1,
        "roles": 12,
        "platforms": 6,
        "posts": 84,
        "audio": 1,
        "failures": [],
        "dry_run": True,
        "with_tts": True,
        "limit": 1,
        "packs": 1,
        "site_files": 20,
        "duration_seconds": 1.5,
        "stages": {name: "ok" for name in STAGES},
        "outputs": {"packs": "output/packs", "posts": "output/posts", "audio": "output/audio", "web": "web",
                    "summary": "output/last_run.json"},
        "ok": True,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 参数透传
def test_dry_run_succeeds_and_builds_the_site(project):
    assert run_daily.main(["--dry-run", "--limit", "2", "--quiet"]) == run_daily.EXIT_OK
    assert (project / "web" / "index.html").is_file(), "验收要求：生成 web/index.html"
    assert (project / "web" / "story").is_dir() and list((project / "web" / "story").glob("*.html"))
    payload = json.loads((project / "output" / "last_run.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True and payload["stories"] == 2 and payload["failures"] == []


def test_json_flag_prints_the_summary(project, capsys):
    assert run_daily.main(["--dry-run", "--limit", "1", "--json", "--quiet"]) == run_daily.EXIT_OK
    printed = capsys.readouterr().out
    payload = json.loads(printed[printed.index("{"): printed.rindex("}") + 1])
    assert payload["mode"] == "dry-run" and payload["stories"] == 1
    assert tuple(payload)[:3] == ("run_at", "mode", "sources")


def test_selection_flags_reach_the_pipeline(project, monkeypatch):
    seen: dict[str, object] = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return fake_summary()

    monkeypatch.setattr("foundry.pipeline.run", spy)
    assert run_daily.main(["--limit", "3", "--roles", "business,kids", "--platforms", "weibo", "--no-tts"]) == 0
    assert seen == {
        "limit": 3,
        "roles": "business,kids",
        "platforms": "weibo",
        "with_tts": False,
        "dry_run": False,
        "min_topic_match": 0.0,
    }


def test_default_flags_request_a_full_live_run(project, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr("foundry.pipeline.run", lambda **kwargs: seen.update(kwargs) or fake_summary())
    run_daily.main([])
    assert seen["dry_run"] is False and seen["with_tts"] is True and seen["limit"] is None
    assert seen["roles"] is None and seen["platforms"] is None


# ---------------------------------------------------------------- 退出码
def test_failures_exit_partial(project, monkeypatch, capsys):
    monkeypatch.setattr("foundry.pipeline.run", lambda **_kwargs: fake_summary(ok=False, failures=["tts: 炸了"]))
    assert run_daily.main(["--dry-run"]) == run_daily.EXIT_PARTIAL
    assert "tts: 炸了" in capsys.readouterr().out


def test_clean_run_exit_code_is_zero(project, monkeypatch):
    monkeypatch.setattr("foundry.pipeline.run", lambda **_kwargs: fake_summary())
    assert run_daily.main(["--dry-run"]) == run_daily.EXIT_OK


def test_startup_problems_exit_two(project, capsys):
    assert run_daily.main(["--dry-run", "--roles", "manager"]) == run_daily.EXIT_ERROR
    assert "未知角色" in capsys.readouterr().err


def test_config_error_also_exits_two(project, monkeypatch, capsys):
    from foundry.config import ConfigError

    def boom(**_kwargs):
        raise ConfigError("没有配置 DASHSCOPE_API_KEY")

    monkeypatch.setattr("foundry.pipeline.run", boom)
    assert run_daily.main(["--limit", "1"]) == run_daily.EXIT_ERROR
    assert "DASHSCOPE_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [(["--limit", "0"], "--limit"), (["--limit", "-2"], "--limit"), (["--min-topic-match", "1.5"], "--min-topic-match")],
)
def test_bad_numbers_are_usage_errors(project, argv, message, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_daily.main(argv)
    assert exit_info.value.code == run_daily.EXIT_ERROR
    assert message in capsys.readouterr().err


def test_exit_code_helper_reads_ok_flag():
    assert run_daily.exit_code(fake_summary(ok=True)) == run_daily.EXIT_OK
    assert run_daily.exit_code(fake_summary(ok=False)) == run_daily.EXIT_PARTIAL


# ---------------------------------------------------------------- 人话摘要
def test_format_summary_lists_counts_and_failures():
    failing = dict(fake_summary()["stages"], master="failed", tts="failed")
    text = run_daily.format_summary(
        fake_summary(ok=False, failures=["master: abc：超时", "tts: 没装 edge-tts"], stages=failing)
    )
    assert "有失败项" in text
    assert "Story 1" in text and "母版 1 份" in text and "配音 1 段" in text
    assert "失败 2 项" in text and "- master: abc：超时" in text and "edge-tts" in text
    assert "master:failed" in text and "site:ok" in text


def test_format_summary_without_failures():
    text = run_daily.format_summary(fake_summary())
    assert "失败 0 项" in text and "dry-run" in text


# ---------------------------------------------------------------- 真子进程（验收命令形状）
def test_subprocess_acceptance_run(project):
    env = {**os.environ, ENV_ROOT: str(project)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--limit", "1", "--quiet"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project),
        timeout=180,
    )
    assert result.returncode == run_daily.EXIT_OK, result.stderr[-1500:]
    assert (project / "web" / "index.html").is_file()
    assert (project / "output" / "last_run.json").is_file()
    assert list((project / "output" / "packs").glob("*.json"))
    assert list((project / "output" / "audio").glob("*.mp3"))
    assert "EnglishFoundry" in (project / "web" / "index.html").read_text(encoding="utf-8")


def test_help_lists_every_documented_flag():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, env=os.environ, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0
    for flag in ("--limit", "--roles", "--no-tts", "--dry-run"):
        assert flag in result.stdout

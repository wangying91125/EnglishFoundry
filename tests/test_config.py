"""foundry.config 行为测试：全部离线，不依赖仓库真实 .env。"""
from __future__ import annotations

import json

import pytest

from foundry.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TTS_RATE,
    DEFAULT_TTS_VOICE,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    ENV_ROOT,
    ConfigError,
    find_project_root,
    load_settings,
)

SOURCES_YAML = """
defaults:
  timeout: 20
  max_age_hours: 24
  user_agent: "EnglishFoundry/1.0 (+https://github.com)"
sources:
  - {id: a_one, name: "A One", category: news, type: rss, enabled: true, url: "https://example.com/a.rss"}
  - {id: b_off, name: "B Off", category: tech, type: rss, enabled: false, url: "https://example.com/b.rss"}
  - {id: c_nocat, url: "https://example.com/c.rss", timeout: 5}
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个用例都从干净环境开始，避免真实环境变量与 .env 串味。"""
    for name in (ENV_API_KEY, ENV_BASE_URL, ENV_MODEL, ENV_ROOT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project(tmp_path):
    """搭一个隔离的项目根：pyproject.toml + config/sources.yaml。"""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "foundry"\n', encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    return tmp_path


def add_yaml(root, filename, text):
    (root / "config" / filename).write_text(text, encoding="utf-8")


def test_defaults_when_only_sources(project):
    settings = load_settings(project)
    assert settings.llm.base_url == DEFAULT_BASE_URL
    assert settings.llm.model == DEFAULT_MODEL
    assert settings.llm.api_key == ""
    assert settings.tts.voice == DEFAULT_TTS_VOICE
    assert settings.tts.rate == DEFAULT_TTS_RATE


def test_yaml_config_is_read(project):
    add_yaml(
        project,
        "settings.yaml",
        "llm:\n  base_url: https://llm.example/v1\n  model: qwen-max\n  temperature: 0.9\n"
        "tts:\n  voice: en-GB-SoniaNeural\n  rate: \"+10%\"\n",
    )
    settings = load_settings(project)
    assert settings.llm.base_url == "https://llm.example/v1"
    assert settings.llm.model == "qwen-max"
    assert settings.llm.temperature == 0.9
    assert settings.tts.voice == "en-GB-SoniaNeural"
    assert settings.tts.rate == "+10%"


def test_env_vars_override_yaml(project, monkeypatch):
    add_yaml(
        project,
        "settings.yaml",
        "llm:\n  base_url: https://from-yaml/v1\n  model: yaml-model\n  api_key: yaml-key\n",
    )
    monkeypatch.setenv(ENV_BASE_URL, "https://from-env/v1")
    monkeypatch.setenv(ENV_MODEL, "env-model")
    monkeypatch.setenv(ENV_API_KEY, "env-key")
    settings = load_settings(project)
    assert settings.llm.base_url == "https://from-env/v1"
    assert settings.llm.model == "env-model"
    assert settings.llm.api_key == "env-key"


def test_dotenv_file_is_loaded(project):
    (project / ".env").write_text(
        "DASHSCOPE_API_KEY=file-key\n"
        "DASHSCOPE_BASE_URL=https://from-dotenv/v1\n"
        "FOUNDRY_MODEL=file-model\n",
        encoding="utf-8",
    )
    settings = load_settings(project)
    assert settings.env_file == project / ".env"
    assert settings.llm.base_url == "https://from-dotenv/v1"
    assert settings.llm.model == "file-model"
    assert settings.llm.api_key == "file-key"


def test_example_placeholder_key_counts_as_unset(project):
    """直接 cp .env.example .env 而没填 Key：视为未配置，且报错要可操作。"""
    (project / ".env").write_text("DASHSCOPE_API_KEY=在此填入你的百炼Key\n", encoding="utf-8")
    settings = load_settings(project)
    assert settings.llm.api_key == ""
    assert "占位值" in settings.llm.masked_key()
    with pytest.raises(ConfigError, match="占位值"):
        settings.llm.require_api_key()
    assert "在此填入你的百炼Key" not in json.dumps(settings.summary(), ensure_ascii=False)


def test_real_env_wins_over_dotenv(project, monkeypatch):
    (project / ".env").write_text("DASHSCOPE_BASE_URL=https://from-dotenv/v1\n", encoding="utf-8")
    monkeypatch.setenv(ENV_BASE_URL, "https://real-env/v1")
    assert load_settings(project).llm.base_url == "https://real-env/v1"


def test_placeholder_expansion_with_fallback(project, monkeypatch):
    add_yaml(
        project,
        "settings.yaml",
        "llm:\n"
        "  base_url: ${DASHSCOPE_BASE_URL:-https://fallback.example/v1}\n"
        "  api_key: ${DASHSCOPE_API_KEY}\n"
        "  model: ${FOUNDRY_MODEL:-qwen-plus}\n",
    )
    settings = load_settings(project)
    assert settings.llm.base_url == "https://fallback.example/v1"
    assert settings.llm.model == "qwen-plus"
    assert settings.llm.api_key == ""

    monkeypatch.setenv(ENV_BASE_URL, "https://expanded.example/v1")
    monkeypatch.setenv(ENV_API_KEY, "expanded-key")
    settings = load_settings(project)
    assert settings.llm.base_url == "https://expanded.example/v1"
    assert settings.llm.api_key == "expanded-key"


def test_all_config_yaml_files_merge(project):
    add_yaml(project, "a_extra.yaml", "llm:\n  temperature: 0.2\n")
    add_yaml(project, "settings.yaml", "llm:\n  model: deep-model\n")
    add_yaml(project, "roles.yaml", "roles:\n  kids:\n    name: 少儿亲子\n    levels: [A1, A2]\n")
    raw = load_settings(project).raw
    assert raw["llm"]["temperature"] == 0.2  # 深度合并：不同键不互相抹掉
    assert raw["llm"]["model"] == "deep-model"
    assert raw["roles"]["kids"]["levels"] == ["A1", "A2"]


def test_sources_get_defaults_and_enabled_filter(project):
    settings = load_settings(project)
    enabled = settings.enabled_sources()
    assert [item["id"] for item in enabled] == ["a_one", "c_nocat"]
    assert enabled[0]["timeout"] == 20
    assert enabled[0]["user_agent"].startswith("EnglishFoundry")
    assert enabled[1]["timeout"] == 5  # 源里写的值优先
    assert enabled[1]["category"] == "news"  # 缺省类别
    assert enabled[1]["name"] == "c_nocat"  # 缺省名称退回 id
    assert len(settings.all_sources()) == 3


def test_paths_are_relative_to_project_root(project):
    settings = load_settings(project)
    assert settings.sources == project / "config" / "sources.yaml"
    assert settings.output.root == project / "output"
    assert settings.output.packs == project / "output" / "packs"
    assert settings.output.audio == project / "output" / "audio"
    assert settings.output.web == project / "web"
    assert settings.output.last_run == project / "output" / "last_run.json"
    settings.output.ensure()
    assert settings.output.packs.is_dir()
    assert settings.rel(settings.output.packs) == "output/packs"


def test_output_dirs_are_configurable(project):
    add_yaml(project, "settings.yaml", "paths:\n  output: dist/output\n  web: dist/web\n")
    output = load_settings(project).output
    assert output.root == project / "dist" / "output"
    assert output.web == project / "dist" / "web"


def test_missing_api_key_raises_actionable_error(project):
    settings = load_settings(project)
    with pytest.raises(ConfigError) as excinfo:
        settings.llm.require_api_key()
    assert ENV_API_KEY in str(excinfo.value)


def test_summary_never_leaks_the_key(project, monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "sk-super-secret-value")
    blob = json.dumps(load_settings(project).summary(), ensure_ascii=False)
    assert "sk-super-secret" not in blob
    assert "已设置" in blob


def test_source_without_url_is_rejected(project):
    add_yaml(project, "zz_broken.yaml", "sources:\n  - {id: nope, name: Nope}\n")
    with pytest.raises(ConfigError):
        load_settings(project)


def test_fetch_and_normalize_sections(project):
    add_yaml(project, "settings.yaml", "fetch:\n  max_attempts: 5\nnormalize:\n  similarity: 0.7\n")
    settings = load_settings(project)
    assert settings.fetch.timeout == 20  # 来自 sources.yaml 的 defaults
    assert settings.fetch.max_attempts == 5  # 来自 settings.yaml
    assert settings.fetch.max_age_hours == 24
    assert settings.normalize.max_age_hours == 24
    assert settings.normalize.similarity == 0.7


def test_foundry_root_overrides_discovery(project, monkeypatch):
    monkeypatch.setenv(ENV_ROOT, str(project))
    assert find_project_root() == project


def test_missing_config_dir_is_tolerated(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "foundry"\n', encoding="utf-8")
    settings = load_settings(root)
    assert settings.source_list == ()
    assert settings.llm.base_url == DEFAULT_BASE_URL

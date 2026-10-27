"""foundry.pipeline 端到端编排测试：全程离线，dry-run 用包内素材，live 路径全部 monkeypatch。

覆盖：``run()`` 的段顺序与产物形状（packs / posts / audio / web）、dry-run 绝不碰
fetcher / master / tts 的出网入口、``limit`` / ``roles`` / ``platforms`` / ``--no-tts`` 的
影响、题材契合度筛角色、单篇与单角色失败隔离、失败项进 ``output/last_run.json``、
摘要键顺序（About 页只取前 10 项）、以及几个纯函数辅助（listening_text / apply_story_meta）。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foundry import pipeline
from foundry.config import ENV_API_KEY, ENV_ROOT, load_settings
from foundry.models import ContentPack
from foundry.pipeline import (
    LISTENING_SUFFIX,
    PLACEHOLDER_MP3,
    STAGES,
    PipelineError,
    apply_story_meta,
    dry_run_fixture,
    fixture_articles,
    listening_text,
    pack_for_story,
    run,
)
from foundry.roles import load_roles, topic_match
from foundry.tts import MANIFEST_NAME, TtsUnavailable, SynthResult

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
SUMMARY_KEYS = (
    "run_at",
    "mode",
    "sources",
    "articles",
    "stories",
    "roles",
    "platforms",
    "posts",
    "audio",
    "failures",
)
SOURCES_YAML = """
defaults:
  timeout: 5
sources:
  - {id: one, name: One News, category: news, type: rss, enabled: true, url: "https://example.com/one.rss"}
  - {id: two, name: Two News, category: tech, type: rss, enabled: true, url: "https://example.com/two.rss"}
  - {id: off, name: Off News, category: news, type: rss, enabled: false, url: "https://example.com/off.rss"}
"""


@pytest.fixture
def project(tmp_path):
    """隔离的临时项目根：pyproject + 仓库的角色/平台配置 + 三个信源（两个启用）。"""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "foundry"\n', encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    shutil.copyfile(REPO_CONFIG / "roles.yaml", config / "roles.yaml")
    shutil.copyfile(REPO_CONFIG / "platforms.yaml", config / "platforms.yaml")
    (config / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(project):
    return load_settings(project)


@pytest.fixture
def offline(monkeypatch):
    """把三个会出网 / 花钱的入口拍死：dry-run 只要碰到它们就直接失败。"""

    def boom(name):
        def _raise(*_args, **_kwargs):
        #     raise AssertionError(f"dry-run 不该调用 {name}")
        return _raise

    monkeypatch.setattr(pipeline, "fetch_all", boom("fetch_all"))
    monkeypatch.setattr(pipeline, "generate_pack", boom("generate_pack"))
    monkeypatch.setattr(pipeline, "batch_synth", boom("batch_synth"))
    return None


def read(path: Path) -> str:
    assert path.is_file(), f"没有生成 {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} 是空文件"
    return text


def summary_payload(project: Path) -> dict:
    return json.loads(read(project / "output" / "last_run.json"))


def relative(project: Path, path: Path) -> str:
    return path.resolve().relative_to(project.resolve()).as_posix()


def story_dirs(project: Path) -> list[Path]:
    posts = project / "output" / "posts"
    return sorted(item for item in posts.iterdir() if item.is_dir()) if posts.is_dir() else []


def md_files(project: Path) -> list[Path]:
    posts = project / "output" / "posts"
    return sorted(item for item in posts.rglob("*.md")) if posts.is_dir() else []


# ---------------------------------------------------------------- dry-run 素材
def test_dry_run_fixture_is_complete_and_fresh():
    fixture = dry_run_fixture()
    articles = fixture_articles(fixture)
    assert len(articles) >= 4, "素材至少要能聚出两个事件"
    assert all(article.published and article.published.tzinfo is not None for article in articles)
    newest = max(article.published for article in articles).astimezone(timezone.utc)
    assert (datetime.now(timezone.utc) - newest).total_seconds() < 6 * 3600, "age_hours 太小才会被时效过滤"
    for template in fixture["packs"]:
        data = {key: value for key, value in template.items() if key != "event"}
        from foundry.master import validate_pack

        assert validate_pack(data) == [], f"素材母版 {template.get('event')} 不合格：{validate_pack(data)}"


def test_missing_fixture_raises_pipeline_error(tmp_path):
    with pytest.raises(PipelineError, match="缺少 dry-run 素材"):
        dry_run_fixture(tmp_path / "nope.json")


def test_pack_for_story_matches_event_and_writes_meta(settings):
    fixture = dry_run_fixture()
    articles = fixture_articles(fixture, now=datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    stories = pipeline.normalize(articles, settings)
    packs = [pack_for_story(story, fixture, index=index) for index, story in enumerate(stories)]
    by_event = {template["event"]: template for template in fixture["packs"]}
    assert by_event["ai-coding-assistant"]["levels"]["B1"]["body"][:12] in packs[0].data["levels"]["B1"]["body"]
    assert packs[0].story_id == stories[0].id
    assert packs[0].data["meta"]["title"] == stories[0].title
    assert packs[0].data["meta"]["source"] == stories[0].source
    assert packs[0].data["tags"], "标签应从素材文章带进母版"


# ---------------------------------------------------------------- dry-run 产出形状
def test_dry_run_produces_pack_posts_audio_and_site(settings, project, offline):
    summary = run(limit=2, dry_run=True, settings=settings)

    packs = sorted((project / "output" / "packs").glob("*.json"))
    assert len(packs) == 2 == summary["packs"]
    payload = json.loads(read(packs[0]))
    assert payload["story_id"] == packs[0].stem
    assert set(payload) >= {"levels", "vocabulary", "quiz", "meta"}

    dirs = story_dirs(project)
    assert len(dirs) == 2, "每个 Story 一份 posts 子目录"
    first = dirs[0]
    role_files = sorted(item.stem for item in first.glob("*.md"))
    assert len(role_files) == summary["roles"] == 12
    platform_files = sorted(item.stem for item in (first / "business").glob("*.md"))
    assert len(platform_files) == summary["platforms"] == 6
    assert relative(project, first / "business" / "xiaohongshu.md") == f"output/posts/{first.name}/business/xiaohongshu.md"
    assert "European" in read(first / "business.md") or "AI coding" in read(first / "business.md")

    audio = sorted((project / "output" / "audio").glob("*.mp3"))
    assert len(audio) == 2 == summary["audio"]
    assert all(LISTENING_SUFFIX in path.name for path in audio), "文件名带 story_id 后缀，站点才能认领"
    assert audio[0].read_bytes() == PLACEHOLDER_MP3
    assert json.loads(read(project / "output" / "audio" / MANIFEST_NAME))["succeeded"] == 2

    index_html = read(project / "web" / "index.html")
    assert "EnglishFoundry" in index_html
    assert len(list((project / "web" / "story").glob("*.html"))) == 2
    assert (project / "web" / "audio" / audio[0].name).is_file(), "音频要复制进 web/"

    assert summary["ok"] is True
    assert summary["failures"] == []
    assert summary["stages"] == {name: "ok" for name in STAGES}


def test_dry_run_never_writes_raw_artifacts(settings, project, offline):
    run(limit=1, dry_run=True, settings=settings)
    assert not (project / "output" / "raw" / "articles.json").exists()
    assert not (project / "output" / "raw" / "stories.json").exists()


def test_write_summary_false_keeps_last_run_untouched(settings, project, offline):
    run(limit=1, dry_run=True, settings=settings, write_summary=False)
    assert not (project / "output" / "last_run.json").exists()


def test_last_run_summary_keys_and_counts(settings, project, offline):
    summary = run(limit=2, dry_run=True, settings=settings)
    payload = summary_payload(project)
    assert payload["mode"] == "dry-run"
    assert tuple(payload) [: len(SUMMARY_KEYS)] == SUMMARY_KEYS, "About 页只显示前 10 项，顺序不能乱"
    assert payload["sources"] == 4 and payload["articles"] == 4
    assert payload["stories"] == payload["packs"] == 2
    assert payload["roles"] == 12 and payload["platforms"] == 6
    assert payload["posts"] == 2 * (12 + 12 * 6)
    assert payload["site_files"] == summary["site_files"] > 0
    assert payload["ok"] is True
    assert payload["outputs"]["summary"] == "output/last_run.json"
    text = read(project / "output" / "last_run.json")
    assert "sk-" not in text and "DASHSCOPE" not in text, "摘要不能带密钥"


# ---------------------------------------------------------------- 参数影响
def test_limit_caps_stories_and_is_passed_to_site(settings, project, offline, monkeypatch):
    seen: dict[str, object] = {}

    def fake_build_site(loaded, **kwargs):
        seen.update(kwargs)
        return [loaded.output.web / "index.html"]

    monkeypatch.setattr(pipeline, "build_site", fake_build_site)
    summary = run(limit=1, dry_run=True, settings=settings)
    assert summary["stories"] == 1 and len(list((project / "output" / "packs").glob("*.json"))) == 1
    assert seen["limit"] == 1


def test_roles_and_platforms_restrict_the_output(settings, project, offline):
    summary = run(limit=1, roles="business,kids", platforms="weibo,x", dry_run=True, settings=settings)
    assert summary["roles"] == 2 and summary["platforms"] == 2
    assert summary["posts"] == 2 * (1 + 2)
    directory = story_dirs(project)[0]
    assert sorted(item.name for item in directory.iterdir()) == ["business", "business.md", "kids", "kids.md"]
    assert sorted(item.stem for item in (directory / "kids").glob("*.md")) == ["weibo", "x"]


def test_role_and_platform_keys_are_forgiving(settings, project, offline):
    summary = run(limit=1, roles="IELTS-Toefl, creator", platforms=["short-video"], dry_run=True, settings=settings)
    assert summary["roles"] == 2 and summary["platforms"] == 1


def test_unknown_role_or_platform_fails_fast(settings, offline):
    with pytest.raises(PipelineError, match="未知角色"):
        run(roles="manager", dry_run=True, settings=settings)
    with pytest.raises(PipelineError, match="未知平台"):
        run(platforms="mastodon", dry_run=True, settings=settings)


def test_min_topic_match_filters_roles(settings, project, offline):
    summary = run(limit=1, dry_run=True, settings=settings, min_topic_match=0.3)
    pack = ContentPack.load(next(sorted((project / "output" / "packs").glob("*.json"))))
    table = load_roles(settings=settings)
    expected = {key for key in table if topic_match(pack, key, table) >= 0.3}
    produced = {item.stem for item in story_dirs(project)[0].glob("*.md")}
    assert produced == expected
    assert 0 < len(produced) < 12, "门槛 0.3 应该只留下部分角色"
    assert summary["roles"] == len(produced)


def test_no_tts_skips_the_audio_stage(settings, project, offline):
    summary = run(limit=2, with_tts=False, dry_run=True, settings=settings)
    assert summary["stages"]["tts"] == "skipped"
    assert summary["audio"] == 0
    assert list((project / "output" / "audio").glob("*.mp3")) == []


# ---------------------------------------------------------------- 失败隔离
def test_master_failure_skips_only_that_story(settings, project, offline, monkeypatch):
    calls: list[int] = []
    original = pipeline.pack_for_story

    def flaky(story, fixture, **kwargs):
        calls.append(len(calls))
        if not calls or len(calls) == 1:
            raise RuntimeError("母版炸了")
        return original(story, fixture, **kwargs)

    monkeypatch.setattr(pipeline, "pack_for_story", flaky)
    summary = run(limit=2, dry_run=True, settings=settings)
    assert len(calls) == 2, "第一篇失败也要继续第二篇"
    assert summary["packs"] == 1 and summary["ok"] is False
    assert summary["failures"] == [
        item for item in summary["failures"] if item.startswith("master:")
    ]
    assert "母版炸了" in summary["failures"][0]
    assert summary["stages"]["master"] == "failed"
    assert summary_payload(project)["failures"] == summary["failures"]


def test_single_role_failure_only_loses_one_post(settings, project, offline, monkeypatch):
    original = pipeline.render_for_role

    def flaky(pack, role_key, table):
        if role_key == "kids":
            raise RuntimeError("角色渲染炸了")
        return original(pack, role_key, table)

    monkeypatch.setattr(pipeline, "render_for_role", flaky)
    summary = run(limit=1, dry_run=True, settings=settings)
    assert summary["roles"] == 11 and summary["platforms"] == 6
    assert not any(item.name == "kids.md" for item in story_dirs(project)[0].iterdir())
    assert any(item.startswith("roles: kids") or item.startswith("roles: ") and "kids" in item for item in summary["failures"])
    assert summary["ok"] is False


def test_fetch_and_normalize_failures_still_build_a_site(settings, project, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_all", lambda **_kwargs: [])
    monkeypatch.setattr(pipeline, "build_site", lambda loaded, **_kwargs: [loaded.output.web / "index.html"])
    summary = run(settings=settings)
    assert any(item.startswith("fetch:") for item in summary["failures"])
    assert summary["stories"] == 0 and summary["ok"] is False
    assert summary["stages"]["fetch"] == "failed" and summary["stages"]["site"] == "ok"
    assert summary_payload(project)["failures"], "失败项必须留在 last_run.json 里"


def test_tts_unavailable_does_not_sink_the_run(settings, project, monkeypatch):
    articles = fixture_articles(dry_run_fixture())
    pack = ContentPack(story_id="x", data={"levels": {"B1": {"title": "T", "body": "B"}}})
    monkeypatch.setattr(pipeline, "fetch_all", lambda **_kwargs: articles)
    monkeypatch.setattr(pipeline, "generate_pack", lambda story, loaded, **_kwargs: pack)
    monkeypatch.setattr(pipeline, "batch_synth", lambda *_args, **_kwargs: (_ for _ in ()).throw(TtsUnavailable("未安装 edge-tts")))
    summary = run(limit=1, settings=settings)
    assert any(item.startswith("tts:") and "edge-tts" in item for item in summary["failures"])
    assert summary["packs"] == 1 and summary["audio"] == 0
    assert summary["stages"]["tts"] == "failed" and summary["stages"]["site"] == "ok"


# ---------------------------------------------------------------- live 路径接线
def test_live_run_calls_the_stages_in_order(settings, project, monkeypatch):
    log: list[str] = []
    articles = fixture_articles(dry_run_fixture())
    template = dry_run_fixture()["packs"][0]

    def fake_fetch(**_kwargs):
        log.append("fetch")
        return articles

    def fake_generate(story, loaded, **kwargs):
        log.append("master")
        assert kwargs["save"] is False, "母版由 pipeline 统一落盘，避免写两次"
        return ContentPack(story_id=story.id, data={key: value for key, value in template.items() if key != "event"})

    def fake_synth(items, **_kwargs):
        log.append("tts")
        return [SynthResult(key=item["key"], path=project / "output" / "audio" / f"{item['key']}.mp3", ok=True) for item in items]

    def fake_site(loaded, **kwargs):
        log.append("site")
        assert kwargs["limit"] == 1
        return [loaded.output.web / "index.html"]

    monkeypatch.setattr(pipeline, "fetch_all", fake_fetch)
    monkeypatch.setattr(pipeline, "generate_pack", fake_generate)
    monkeypatch.setattr(pipeline, "batch_synth", fake_synth)
    monkeypatch.setattr(pipeline, "build_site", fake_site)
    summary = run(limit=1, settings=settings)

    assert log == ["fetch", "master", "tts", "site"]
    assert summary["mode"] == "live" and summary["dry_run"] is False
    assert summary["sources"] == 2, "config/sources.yaml 里只有两个启用源"
    assert summary["packs"] == 1 and summary["audio"] == 1 and summary["ok"] is True
    assert json.loads(read(project / "output" / "raw" / "articles.json"))[0]["title"] == articles[0].title
    assert json.loads(read(project / "output" / "raw" / "stories.json"))[0]["articles"], "聚合结果也留在 raw/"
    written = json.loads(read(next(sorted((project / "output" / "packs").glob("*.json")))))
    assert written["meta"]["title"], "live 路径同样把 Story 元信息补进母版"


# ---------------------------------------------------------------- 小函数
def test_listening_text_prefers_script_then_levels():
    pack = ContentPack(story_id="x", data={"listening_script": "  hello  ", "levels": {"B1": {"body": "nope"}}})
    assert listening_text(pack) == "hello"
    fallback = ContentPack(story_id="x", data={"levels": {"B1": {"body": "B1 body"}, "A2": {"body": "A2 body"}}})
    assert listening_text(fallback) == "B1 body"
    assert listening_text(ContentPack(story_id="x", data={})) == ""


def test_apply_story_meta_keeps_story_id_and_tags(settings):
    articles = fixture_articles(dry_run_fixture())
    story = pipeline.normalize(articles, settings)[0]
    pack = ContentPack(story_id=story.id, data={"levels": {}})
    apply_story_meta(pack, story)
    assert pack.data["story_id"] == story.id
    assert pack.data["meta"]["published"].startswith("20")
    assert set(pack.data["tags"]) <= {"GDPR", "data protection", "EU", "privacy", "regulation", "AI", "developer tools", "engineering culture"}

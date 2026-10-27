"""foundry.site 测试：全程离线，用 ``tests/fixtures/sample_pack.json`` 当母版，不联网、不调 LLM、不配音。

覆盖：``build_site`` 的文件清单与首页（今日精选 / 角色与类别筛选 / 客户端搜索索引）、
故事详情页九个板块与音频播放器 + 下载入口、角色页（优先认领 ``output/posts`` 的现成 Markdown 与平台文案）、
about 页的流水线与信源、静态资产复制、空产物 / 坏 JSON / 缺模板的降级、Markdown 渲染的转义安全，
以及命令行入口。断言全部打在 HTML 关键字上，改模板时能一眼看出破了哪一块。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from foundry import site
from foundry.config import ENV_ROOT, load_settings
from foundry.site import (
    AudioClip,
    SiteError,
    build_site,
    discover_posts,
    load_audio_clips,
    markdown_to_html,
    story_from_pack,
)
from foundry.roles import load_roles
from foundry.site import _main as site_main
from foundry.tts import MANIFEST_NAME

FIXTURE = Path(__file__).parent / "fixtures" / "sample_pack.json"
REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIXTURE_TITLE = "European regulators fine a social network 430 million euros over data transfers"
FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 120

SOURCES_YAML = """
defaults:
  timeout: 20
sources:
  - {id: reuters, name: Reuters, category: business, type: rss, enabled: true, url: "https://example.com/reuters.rss"}
  - {id: ap, name: Associated Press, category: news, type: rss, enabled: true, url: "https://example.com/ap.rss"}
  - {id: off, url: "https://example.com/off.rss", enabled: false}
"""


@pytest.fixture
def project(tmp_path):
    """隔离的临时项目根：pyproject + 仓库的 roles/platforms 配置 + 两个信源。"""
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


def web_root(project: Path) -> Path:
    return (project / "web").resolve()


def write_pack(project: Path, story_id: str, **meta) -> Path:
    """把 fixture 母版复制成 ``output/packs/<story_id>.json``，可附带元信息。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["story_id"] = story_id
    data.update(meta)
    target = project / "output" / "packs" / f"{story_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


def read(path: Path) -> str:
    assert path.is_file(), f"没有生成 {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} 是空文件"
    return text


def relative_names(project: Path, files) -> set[str]:
    root = project.resolve()
    return {path.resolve().relative_to(root).as_posix() for path in files}


def put_audio(project: Path, items: list[dict], extra: dict[str, bytes] | None = None) -> None:
    """按 foundry.tts 的形状写 manifest + mp3（不联网，字节是假的）。"""
    audio = project / "output" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    for row in items:
        if row.get("ok") is False:
            continue  # 失败的条目连文件都不该有（tts 是原子写盘）
        (audio / row["file"]).write_bytes(FAKE_MP3)
    (audio / MANIFEST_NAME).write_text(
        json.dumps({"generated_at": "2026-10-27T00:00:00+00:00", "total": len(items), "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    for name, payload in (extra or {}).items():
        (audio / name).write_bytes(payload)


# ---------------------------------------------------------------- 文件清单
def test_build_site_returns_generated_file_manifest(settings, project):
    files = build_site(settings, packs=[FIXTURE])
    assert files, "build_site 必须返回生成文件清单"
    names = relative_names(project, files)
    assert {"web/index.html", "web/about.html", "web/static/style.css", "web/static/app.js"} <= names
    assert "web/story/demo-eu-data-fine.html" in names
    role_pages = {name for name in names if name.startswith("web/role/")}
    assert len(role_pages) == 12, "12 个角色都要有专页"
    assert all(path.read_text(encoding="utf-8").strip() for path in files)


def test_build_site_reads_packs_from_output_dir(settings, project):
    write_pack(project, "demo-eu-data-fine")
    files = build_site(settings)
    assert (web_root(project) / "story" / "demo-eu-data-fine.html").resolve() in files


def test_build_site_accepts_dicts_and_paths(settings):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    files = build_site(settings, packs=[payload, FIXTURE])
    index = read(web_root(settings.root) / "index.html")
    assert index.count('class="card story-card"') == 2  # 同一母版的两种喂法都认


# ---------------------------------------------------------------- 首页
def test_index_shows_todays_pick_and_filters(settings):
    build_site(settings, packs=[FIXTURE])
    index = read(web_root(settings.root) / "index.html")
    assert "<!DOCTYPE html>" in index and 'lang="en"' in index
    assert "今日精选" in index and "Today" in index
    assert FIXTURE_TITLE in index
    assert "Start reading 开始精读" in index
    assert 'data-filter-group="role"' in index and 'data-filter-group="category"' in index
    assert 'data-filter-value="business"' in index
    assert "Roles 角色" in index and "Categories 类别" in index
    assert 'href="about.html"' in index


def test_index_search_is_client_side_only(settings):
    build_site(settings, packs=[FIXTURE])
    index = read(web_root(settings.root) / "index.html")
    assert 'type="search"' in index and "data-search" in index
    assert 'src="static/app.js"' in index and 'href="static/style.css"' in index
    card = index.split('class="card story-card"', 1)[1]
    attrs = card.split(">", 1)[0]
    assert "regulator" in attrs  # 生词进了检索索引
    assert "欧盟" in attrs  # 中文背景也可搜
    assert 'data-category="news"' in attrs
    assert 'data-roles="' in attrs and "business" in attrs
    for forbidden in ("XMLHttpRequest", "fetch(", "cdn.", "https://fonts."):
        assert forbidden not in index, "纯静态站点不该有外部依赖或运行时请求"


def test_index_lists_both_categories_and_counts(settings):
    write_pack(project := settings.root, "demo-eu-data-fine", category="news")
    write_pack(project, "demo-asia-ports", category="tech", title="Asia rebuilds its ports for greener cargo")
    files = build_site(settings)
    index = read(web_root(project) / "index.html")
    assert 'data-filter-value="tech"' in index and 'data-filter-value="news"' in index
    assert "Asia rebuilds its ports" in index
    assert "2 stories 篇" in index
    assert (web_root(project) / "story" / "demo-asia-ports.html") in files


# ---------------------------------------------------------------- 详情页
def test_story_page_has_every_module(settings):
    build_site(settings, packs=[FIXTURE])
    page = read(web_root(settings.root) / "story" / "demo-eu-data-fine.html")
    for anchor in ("id=\"reading\"", "id=\"background\"", "id=\"vocabulary\"", "id=\"bilingual\"",
                   "id=\"grammar\"", "id=\"script\"", "id=\"speaking\"", "id=\"writing\"", "id=\"quiz\""):
        assert anchor in page, f"详情页缺板块 {anchor}"
    assert page.count('role="tab"') == 6, "六档 CEFR 各自可切"
    assert 'data-level-panel="C2"' in page
    assert page.count('class="word"') == 12  # 词表 12 行
    assert page.count('class="quiz-q"') == 5 and 'data-answer="B"' in page
    assert "The press conference" in page  # 口语角色扮演
    assert "Model essay" in page and "quiz-explain" in page


def test_story_page_links_role_and_neighbours(settings):
    write_pack(project := settings.root, "demo-eu-data-fine", category="news")
    write_pack(project, "demo-asia-ports", category="tech", title="Asia rebuilds its ports for greener cargo", published="2026-10-26T08:00:00+00:00")
    build_site(settings)
    page = read(web_root(project) / "story" / "demo-eu-data-fine.html")
    assert "../role/" in page and "../index.html#library" in page
    assert "More stories 更多篇目" in page and "demo-asia-ports.html" in page
    assert 'class="level-tab' in page and 'role="tab"' in page


# ---------------------------------------------------------------- 音频
def test_story_and_index_expose_player_and_download(settings):
    put_audio(
        settings.root,
        [
            {"key": "demo-eu-data-fine_business", "file": "demo-eu-data-fine_business.mp3", "voice": "en-US-GuyNeural",
             "rate": "+5%", "bytes": len(FAKE_MP3), "seconds": 96.4, "ok": True},
        ],
        extra={"stray-clip.mp3": FAKE_MP3[:200]},
    )
    files = build_site(settings, packs=[FIXTURE])
    index = read(web_root(settings.root) / "index.html")
    story = read(web_root(settings.root) / "story" / "demo-eu-data-fine.html")
    assert "<audio controls" in index and "Download MP3 下载" in index
    assert "Listening library 听力下载" in index and "Download 下载" in index
    assert 'id="listening"' in story and "player-card" in story
    assert "Speed 倍速" in story and 'data-speed="0.75"' in story
    assert "../audio/demo-eu-data-fine_business.mp3" in story
    copied = web_root(settings.root) / "audio" / "demo-eu-data-fine_business.mp3"
    assert copied in files and copied.read_bytes() == FAKE_MP3
    assert "商务职场" in story  # 角色名映射进音频标签


def test_load_audio_clips_uses_manifest_and_scans_strays(settings):
    put_audio(
        settings.root,
        [{"key": "demo-eu-data-fine_listening", "file": "demo-eu-data-fine_listening.mp3", "bytes": len(FAKE_MP3),
          "seconds": 12.5, "voice": "en-GB-SoniaNeural", "rate": "-10%", "ok": True},
         {"key": "broken", "file": "broken.mp3", "ok": False}],  # 失败条目不进站点
        extra={"unlisted.mp3": FAKE_MP3, "empty.mp3": b""},
    )
    clips = load_audio_clips(settings)
    by_name = {clip.filename: clip for clip in clips}
    assert set(by_name) == {"demo-eu-data-fine_listening.mp3", "unlisted.mp3"}
    clip = by_name["demo-eu-data-fine_listening.mp3"]
    assert (clip.seconds, clip.size, clip.voice, clip.rate) == (12.5, len(FAKE_MP3), "en-GB-SoniaNeural", "-10%")
    assert clip.duration_label == "0:13" and clip.size_label.endswith("KB") and clip.url.startswith("audio/")
    assert isinstance(clip, AudioClip)


def test_audio_can_be_turned_off(settings):
    put_audio(settings.root, [{"key": "demo-eu-data-fine", "file": "demo-eu-data-fine.mp3", "bytes": len(FAKE_MP3), "ok": True}])
    files = build_site(settings, packs=[FIXTURE], include_audio=False)
    assert not any(path.name.endswith(".mp3") for path in files)
    assert "<audio controls" not in read(web_root(settings.root) / "index.html")


# ---------------------------------------------------------------- 角色页与文案认领
def test_role_pages_render_persona_markdown(settings):
    build_site(settings, packs=[FIXTURE])
    page = read(web_root(settings.root) / "role" / "business.html")
    assert "商务职场" in page and "business" in page
    assert "核心词汇" in page, "角色正文要渲染成 HTML"
    assert "word_limit" not in page  # 配置字段不直接外泄
    assert "Topics it looks for 题材关键词" in page
    assert "Daily task 收尾任务" in page
    assert "demo-eu-data-fine.html" in page


def test_role_page_prefers_posts_markdown_and_platform_copies(settings):
    posts = settings.root / "output" / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    (posts / "business.md").write_text("# MANUAL-ROLE-DRAFT\n\n| word | 中文 |\n| --- | --- |\n| tariff | 关税 |\n\n<details>\n<summary>答案</summary>\n1. A\n</details>\n", encoding="utf-8")
    (posts / "business" ).mkdir(exist_ok=True)
    (posts / "business" / "xiaohongshu.md").write_text("MANUAL-PLATFORM-COPY 🍠 3 个词\n", encoding="utf-8")
    build_site(settings, packs=[FIXTURE])
    page = read(web_root(settings.root) / "role" / "business.html")
    assert "MANUAL-ROLE-DRAFT" in page
    assert "MANUAL-PLATFORM-COPY" in page and "Publishing drafts 平台文案" in page
    assert "<table>" in page and "<details>" in page  # 表格与折叠块都还原成 HTML
    story = read(web_root(settings.root) / "story" / "demo-eu-data-fine.html")
    assert "MANUAL-PLATFORM-COPY" in story


def test_discover_posts_uses_story_and_role_directories(settings):
    posts = settings.root / "output" / "posts"
    (posts / "demo-eu-data-fine" / "cet").mkdir(parents=True)
    (posts / "demo-eu-data-fine" / "cet" / "weibo.md").write_text("STORY-SCOPED-COPY", encoding="utf-8")
    (posts / "k12.md").write_text("GENERIC-ROLE-MARKDOWN", encoding="utf-8")
    (posts / "notes.txt").write_text("ignored", encoding="utf-8")
    found = discover_posts(settings=settings, role_keys=["cet", "k12"], platform_keys=["weibo"], story_ids=["demo-eu-data-fine"])
    assert found[("demo-eu-data-fine", "cet")]["platforms"]["weibo"] == "STORY-SCOPED-COPY"
    assert found[("*", "k12")]["markdown"] == "GENERIC-ROLE-MARKDOWN"
    assert all("*" != key[0] or key[1] == "k12" for key in found)


# ---------------------------------------------------------------- about
def test_about_page_documents_pipeline_and_sources(settings):
    build_site(settings, packs=[FIXTURE])
    about = read(web_root(settings.root) / "about.html")
    for stage in ("fetch", "normalize", "master", "roles", "platforms", "tts", "site"):
        assert f"<code>{stage}</code>" in about
    assert "关于本站" in about and "无后端" in about
    assert "Reuters" in about and "Associated Press" in about
    assert "disabled" not in about.lower()  # 只列 enabled 的源
    assert "python -m foundry.site" in about
    assert about.count('scope="row"') >= 12  # 12 个角色的表格


def test_about_page_shows_last_run_summary(settings):
    (settings.root / "output").mkdir(parents=True, exist_ok=True)
    (settings.root / "output" / "last_run.json").write_text(
        json.dumps({"generated_at": "2026-10-27T00:00:00+00:00", "sources": 3, "stories": 2, "failed": []}), encoding="utf-8"
    )
    build_site(settings, packs=[FIXTURE])
    about = read(web_root(settings.root) / "about.html")
    assert "Last pipeline run · 上次运行" in about
    assert "generated_at" in about and "2026-10-27T00:00:00+00:00" in about


# ---------------------------------------------------------------- 降级与清理
def test_index_without_packs_still_builds(settings):
    files = build_site(settings)
    index = read(web_root(settings.root) / "index.html")
    assert "No content packs yet" in index
    assert "今日精选" in index
    assert any(path.name == "about.html" for path in files)


def test_broken_pack_is_skipped_not_fatal(settings):
    packs = settings.root / "output" / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    (packs / "broken.json").write_text("{not json", encoding="utf-8")
    shutil.copyfile(FIXTURE, packs / "demo-eu-data-fine.json")
    files = build_site(settings)
    assert (web_root(settings.root) / "story" / "demo-eu-data-fine.html") in files
    assert not (web_root(settings.root) / "story" / "broken.html").exists()


def test_limit_and_prune(settings):
    write_pack(project := settings.root, "demo-eu-data-fine", category="news")
    write_pack(project, "demo-asia-ports", category="tech")
    build_site(settings, limit=1)
    stories = web_root(project) / "story"
    assert len(list(stories.glob("*.html"))) == 1
    build_site(settings)
    assert len(list(stories.glob("*.html"))) == 2
    build_site(settings, limit=1, prune=False)
    assert len(list(stories.glob("*.html"))) == 2, "--no-prune 保留旧页面"
    build_site(settings, limit=1)
    assert len(list(stories.glob("*.html"))) == 1, "默认清理上一版遗留页面"


def test_missing_templates_raise_site_error(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(site, "PACKAGE_ROOT", tmp_path / "nowhere")
    with pytest.raises(SiteError, match="模板"):
        build_site(settings, packs=[FIXTURE])


# ---------------------------------------------------------------- 母版与 Markdown
def test_story_from_pack_falls_back_on_missing_metadata():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    story = story_from_pack(payload)
    assert isinstance(story, site.SiteStory)
    assert story.id == "pack" or story.id == "demo-eu-data-fine"
    assert story.title == FIXTURE_TITLE  # 取 B1 档标题当大标题
    assert story.default_level == "B1" and story.category == "news"
    assert story.summary and story.date_label  # 摘要与日期都有兜底
    assert {item["level"] for item in story.levels} == {"A1", "A2", "B1", "B2", "C1", "C2"}


def test_story_from_pack_reads_metadata_and_quiz_shapes():
    roles = load_roles(REPO_CONFIG / "roles.yaml")
    story = story_from_pack(
        {
            "story_id": "odd-shapes",
            "title": "Odd shapes still render",
            "category": "Tech",
            "source": "Wired",
            "published": "2026-01-05",
            "url": "https://example.com/odd",
            "levels": {"A1": {"title": "Odd", "body": "One two three four five."}},
            "quiz": [{"question": "Which?", "options": {"A": "alpha", "B": "beta"}, "answer": 2}],
            "speaking": {"questions": ["ask me"], "roleplay": []},
            "writing": "A model essay that is long enough to count as a sample for the students today.",
        },
        roles=roles,
    )
    assert story.title == "Odd shapes still render"
    assert (story.category, story.source, story.date_label) == ("tech", "Wired", "2026-01-05")
    assert story.quiz[0]["choices"][0] == {"letter": "A", "text": "alpha"}
    assert story.quiz[0]["answer"] == "B", "数字答案按 1 基位置换算成字母"
    assert story.writing["essay"].startswith("A model essay")
    assert story.href == "story/odd-shapes.html"
    assert story.role_pages, "只有 A1 的母版也要按难度偏好匹配到角色"
    assert all(tag["href"].startswith("role/") and tag["name"] for tag in story.role_pages)


def test_markdown_to_html_renders_structure():
    html = str(
        markdown_to_html(
            """# Title 标题

> quote line

## 📚 核心词汇

| word | 中文 |
| --- | --- |
| **tariff** | 关税 |
| quota | 配额 |

1. first
2. second

- bullet

<details>
<summary>答案</summary>
1. A
</details>

---
*italic tail* [link](https://example.com/a)
"""
        )
    )
    assert "<h2>Title 标题</h2>" in html and "<h3>📚 核心词汇</h3>" in html
    assert "<blockquote><p>quote line</p></blockquote>" in html
    assert '<div class="table-wrap"><table>' in html and "<strong>tariff</strong>" in html
    assert "<ol><li>first</li><li>second</li></ol>" in html
    assert "<ul><li>bullet</li></ul>" in html
    assert "<details>" in html and "<summary>答案</summary>" in html
    assert "<hr>" in html and "<em>italic tail</em>" in html
    assert 'href="https://example.com/a"' in html


def test_markdown_to_html_escapes_untrusted_input():
    html = str(
        markdown_to_html(
            '<script>alert("xss")</script>\n\n'
            '[click](javascript:alert(1))\n\n'
            '<div class="bad">raw</div>\n\n'
            '`<b>code</b>` and A < B'
        )
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="#"' in html and "javascript:" not in html
    assert "<div" not in html
    assert "<code>&lt;b&gt;code&lt;/b&gt;</code>" in html and "A &lt; B" in html


# ---------------------------------------------------------------- 命令行
def test_cli_main_prints_file_list(project, monkeypatch, capsys):
    monkeypatch.setenv(ENV_ROOT, str(project))
    assert site_main(["--packs", str(FIXTURE), "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "共生成" in out
    assert "web/index.html" in out and "web/about.html" in out and "web/story/demo-eu-data-fine.html" in out
    assert (web_root(project) / "index.html").is_file()


def test_cli_main_reports_missing_templates(project, monkeypatch, capsys):
    monkeypatch.setenv(ENV_ROOT, str(project))
    monkeypatch.setattr(site, "PACKAGE_ROOT", project / "nowhere")
    assert site_main(["--packs", str(FIXTURE)]) == 1
    assert "模板" in capsys.readouterr().err


def test_prune_removes_stale_audio_copies(settings):
    put_audio(settings.root, [{"key": "demo-eu-data-fine", "file": "demo-eu-data-fine.mp3", "bytes": len(FAKE_MP3), "ok": True}])
    build_site(settings, packs=[FIXTURE])
    copied = web_root(settings.root) / "audio" / "demo-eu-data-fine.mp3"
    assert copied.is_file()
    for path in (settings.root / "output" / "audio").glob("*.mp3"):
        path.unlink()  # 源音频被清掉（重跑流水线时很常见）
    build_site(settings, packs=[FIXTURE])
    assert not copied.exists(), "站点里的过期音频副本该被 prune 掉"

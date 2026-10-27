"""foundry.platforms 测试：离线，用 tests/fixtures/sample_pack.json 的角色渲染当输入，不联网、不调 LLM。

覆盖：config/platforms.yaml 的 6 个平台与字段校验、同一角色 Markdown 在 6 个平台的
char_limit/格式验收、中文框架+英文学习内容 vs 全英文的语言纪律、meta 覆盖、紧预算与
兜底输入（空 Markdown / 只有一行标题）、配置非法报错与命令行入口。
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from foundry.config import ENV_ROOT
from foundry.models import ContentPack
from foundry.platforms import (
    EXPECTED_PLATFORMS,
    PLATFORM_LANGUAGES,
    PLATFORM_UNITS,
    PLATFORMS_FILE_NAME,
    PlatformsError,
    count_chars,
    load_platforms,
    platform_for,
    platforms_path,
    render_all_platforms,
    render_for_platform,
)
from foundry.platforms import _main as platforms_main
from foundry.roles import render_for_role

FIXTURE = Path(__file__).parent / "fixtures" / "sample_pack.json"
REPO_PLATFORMS = Path(__file__).resolve().parents[1] / "config" / PLATFORMS_FILE_NAME
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
META = {"url": "https://example.com/eu-data-fine", "source": "Reuters", "date": "2026-10-27"}
BASE_PLATFORM = "name: A\n    char_limit: 200\n    "

#: 约定 8 字段之外的长度口径（与 config/platforms.yaml 保持一致）
LIMITS: dict[str, tuple[str, str, int]] = {
    "xiaohongshu": ("zh", "chars", 600),
    "wechat_mp": ("zh", "chars", 3000),
    "shortvideo": ("zh", "chars", 320),
    "weibo": ("zh", "chars", 140),
    "x": ("en", "chars", 280),
    "linkedin": ("en", "words", 600),
}


@pytest.fixture(scope="module")
def pack() -> ContentPack:
    """母版 fixture（和 output/packs/*.json 同形状）。"""
    return ContentPack.load(FIXTURE)


@pytest.fixture(scope="module")
def platforms() -> dict:
    """仓库里的 6 个平台。"""
    return load_platforms(REPO_PLATFORMS)


@pytest.fixture(scope="module")
def business_md(pack) -> str:
    """验收主输入：business 角色的完整 Markdown（正文/词汇/口语/写作/练习都在）。"""
    return render_for_role(pack, "business")


def write_platforms(tmp_path: Path, text: str) -> Path:
    """在临时项目根里写一份 config/platforms.yaml 并返回路径。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / PLATFORMS_FILE_NAME
    target.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------- 平台配置
def test_load_platforms_has_the_six_expected_keys(platforms):
    assert list(platforms) == list(EXPECTED_PLATFORMS)
    assert len(EXPECTED_PLATFORMS) == 6


def test_every_platform_has_all_required_fields(platforms):
    for key, platform in platforms.items():
        assert platform.key == key
        assert platform.name and platform.format and platform.title_rule, key
        assert platform.notes, key
        assert platform.emoji, key
        assert isinstance(platform.hashtags, tuple), key
        assert platform.char_limit > 0 and platform.unit in PLATFORM_UNITS, key
        assert platform.language in PLATFORM_LANGUAGES, key


def test_yaml_declares_the_agreed_limits_and_languages(platforms):
    for key, (language, unit, limit) in LIMITS.items():
        platform = platforms[key]
        assert (platform.language, platform.unit, platform.char_limit) == (language, unit, limit), key
    linkedin = platforms["linkedin"]
    assert linkedin.char_min == 300, "LinkedIn 约定 300-600 词"
    assert platforms["weibo"].hashtags[:2] == ("英语", "外刊")
    assert 1 <= len(platforms["xiaohongshu"].hashtags) <= 6


def test_count_chars_skips_newlines():
    assert count_chars("ab\n cd") == 5
    assert count_chars("") == 0


def test_platforms_path_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    assert platforms_path(tmp_path / "mine.yaml") == tmp_path / "mine.yaml"
    assert platforms_path("config/platforms.yaml") == tmp_path / "config" / PLATFORMS_FILE_NAME
    assert platforms_path().parent.parent == tmp_path  # 默认 <root>/config/platforms.yaml


def test_platform_for_is_lenient_on_keys_and_strict_about_unknowns(platforms):
    assert platform_for("WeChat-MP", platforms).key == "wechat_mp"
    assert platform_for(" X ", platforms).key == "x"
    with pytest.raises(PlatformsError, match="没有这个平台"):
        platform_for("douyin", platforms)


def test_missing_platforms_file_raises(tmp_path):
    with pytest.raises(PlatformsError, match="找不到平台配置"):
        load_platforms(tmp_path / "config" / PLATFORMS_FILE_NAME)


@pytest.mark.parametrize("payload", ["", "hello", "platforms: 3", "platforms: {}"])
def test_useless_platforms_files_raise_readable_errors(tmp_path, payload):
    with pytest.raises(PlatformsError, match="platforms|空|映射"):
        load_platforms(write_platforms(tmp_path, payload))


def test_broken_yaml_reports_the_file(tmp_path):
    with pytest.raises(PlatformsError, match="不是合法 YAML"):
        load_platforms(write_platforms(tmp_path, "platforms:\n  a: {name: 缺括号\n"))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("unit: pages\n", "unit"),
        ("language: fr\n", "language"),
        ("char_limit: 0\n", "char_limit"),
        ("char_limit: abc\n", "char_limit"),
        ("char_min: 500\n", "char_min"),
        ("hashtags: {a: b}\n", "hashtags"),
    ],
)
def test_bad_platform_fields_raise_readable_errors(tmp_path, body, message):
    path = write_platforms(tmp_path, f"platforms:\n  a:\n    {BASE_PLATFORM}{body}")
    with pytest.raises(PlatformsError, match=message):
        load_platforms(path)


def test_entry_without_any_identifier_raises(tmp_path):
    path = write_platforms(tmp_path, "platforms:\n  - {char_limit: 100}\n")
    with pytest.raises(PlatformsError, match="无法确定平台标识"):
        load_platforms(path)


def test_duplicate_platform_key_raises(tmp_path):
    path = write_platforms(tmp_path, "platforms:\n  - {key: a, char_limit: 100}\n  - {key: a, char_limit: 200}\n")
    with pytest.raises(PlatformsError, match="重复"):
        load_platforms(path)


def test_platforms_can_be_a_list_of_entries(tmp_path):
    path = write_platforms(tmp_path, "platforms:\n  - {key: telegraph, name: TG, char_limit: 1024, unit: chars}\n")
    table = load_platforms(path)
    assert list(table) == ["telegraph"]


# ---------------------------------------------------------------- 六平台验收（同一份角色 Markdown）
def test_all_six_platforms_return_copy_within_their_limits(business_md, platforms):
    """核心验收：同一角色 markdown，6 个平台都返回非空且符合各自 char_limit 的文案。"""
    rendered = render_all_platforms(business_md, META, platforms=platforms)
    assert list(rendered) == list(EXPECTED_PLATFORMS)
    for key, text in rendered.items():
        platform = platforms[key]
        assert text.strip(), key
        assert platform.fits(text), (key, platform.count(text), platform.char_limit, platform.unit)
        assert text.endswith("\n"), key


@pytest.mark.parametrize("role_key", ["business", "kids", "creator"])
def test_every_role_markdown_renders_fitting_copy_for_all_platforms(pack, platforms, role_key):
    markdown = render_for_role(pack, role_key)
    for key, platform in platforms.items():
        text = render_for_platform(markdown, META, key, platforms)
        assert platform.fits(text), (role_key, key, platform.count(text), platform.char_limit)


def test_render_is_deterministic(business_md, platforms):
    for key in EXPECTED_PLATFORMS:
        first = render_for_platform(business_md, META, key, platforms)
        assert first == render_for_platform(business_md, META, key, platforms), key


# ---------------------------------------------------------------- 各平台格式
def test_xiaohongshu_has_hook_headline_wordcards_and_topics(business_md, platforms):
    text = render_for_platform(business_md, META, "xiaohongshu", platforms)
    assert text.startswith("🍠"), "钩子标题要带平台 emoji 开头"
    assert "📌 今日英文头条｜European regulators fine" in text, "英文头条要原样进文案"
    assert "🧠 词卡直接抄进备忘录（" in text
    assert re.search(r"1️⃣ regulator n\.｜监管机构", text)
    assert "#英语学习" in text and "#外刊精读" in text
    assert len(re.findall(r"#\S+", text)) >= 3, "话题标签至少 3 个"
    assert count_chars(text) <= 600


def test_wechat_mp_is_a_sectioned_long_read(business_md, platforms):
    text = render_for_platform(business_md, META, "wechat_mp", platforms)
    assert text.startswith("# 📰"), "深度长文要有主标题"
    assert "## 一、事件原文（B1）" in text
    assert "## 二、核心词汇精讲（" in text and "- 英文释义：" in text
    assert "Regulators in the European Union have fined" in text, "英文原文必须完整进正文节"
    assert "今日任务" in text, "收尾任务要带上角色的 cta"
    assert re.search(r"## [一二三四五六七]、", text)
    sections = re.findall(r"^## ", text, re.M)
    assert len(sections) >= 3, "小标题分层"
    assert count_chars(text) <= 3000


def test_shortvideo_is_a_spoken_script_with_an_opening_hook(business_md, platforms):
    text = render_for_platform(business_md, META, "shortvideo", platforms)
    assert text.startswith("【3秒钩子】"), "前 3 秒钩子必须是第一行"
    assert "【跟读】" in text and "【结尾】" in text
    assert "【核心词】regulator（监管机构）" in text
    assert "|" not in text and "---" not in text, "口播稿不留表格语法"
    assert count_chars(text) <= 320, "≈60 秒语速上限"


def test_weibo_wraps_topics_and_stays_in_140(business_md, platforms):
    text = render_for_platform(business_md, META, "weibo", platforms)
    assert text.startswith("#英语# #外刊#"), "微博 #话题# 语法且标签不被截断"
    assert "European regulators fine" in text
    assert count_chars(text) <= 140


def test_weibo_keeps_the_link_when_there_is_room(platforms):
    text = render_for_platform("# EU Fines App\n", META, "weibo", platforms)
    assert "详情→https://example.com/eu-data-fine" in text


def test_x_is_english_short_with_one_or_two_hashtags(business_md, platforms):
    text = render_for_platform(business_md, META, "x", platforms)
    assert len(text) - text.count("\n") <= 280
    assert not CJK_RE.search(text), "英文平台不许漏中文"
    tags = re.findall(r"#\w+", text)
    assert 1 <= len(tags) <= 2, f"hashtag 要 1-2 个，实际 {tags}"
    assert "#LearnEnglish" in tags or "#EuropeanRegulators" in tags
    assert "European regulators fine" in text


def test_linkedin_is_a_professional_post_with_a_cta(business_md, platforms):
    platform = platforms["linkedin"]
    text = render_for_platform(business_md, META, "LinkedIn", platforms)  # key 大小写不敏感
    words = platform.count(text)
    assert 300 <= words <= 600, "LinkedIn 约定 300-600 词"
    assert not CJK_RE.search(text), "英文平台不许漏中文"
    for marker in ("What happened", "Why this matters", "Vocabulary to know", "Follow EnglishFoundry"):
        assert marker in text, marker
    assert re.search(r"• regulator \(n\) — .*In context:", text), "词单要带释义和例句"
    assert text.rstrip("\n").splitlines()[-1].startswith("#"), "结尾 CTA 后接 hashtags"
    assert ".." not in text, "句号别叠双"


# ---------------------------------------------------------------- 语言纪律与 meta
def test_chinese_platforms_pair_chinese_frame_with_english_content(business_md, platforms):
    zh_keys = [key for key, platform in platforms.items() if platform.is_chinese]
    assert zh_keys == ["xiaohongshu", "wechat_mp", "shortvideo", "weibo"]
    for key in zh_keys:
        text = render_for_platform(business_md, META, key, platforms)
        assert CJK_RE.search(text), f"{key} 应有中文框架"
        assert re.search(r"[A-Za-z]{5,}", text), f"{key} 应有英文学习内容"


def test_meta_title_and_tags_override_the_markdown(platforms):
    minimal = "# Old headline\n\n## 📖 正文 · CEFR B1\n\nSome English body sentence.\n"
    text = render_for_platform(minimal, {"title": "Meta headline wins"}, "x", platforms)
    assert "Meta headline wins" in text and "Old headline" not in text
    zh = render_for_platform(minimal, {"tags": ["climate", "energy"]}, "xiaohongshu", platforms)
    assert "#Climate" in zh and "#Energy" in zh


@pytest.mark.parametrize("key", EXPECTED_PLATFORMS)
def test_bare_or_empty_input_still_returns_fitting_copy(platforms, key):
    platform = platforms[key]
    for markdown in ("", "# Only a headline\n"):
        text = render_for_platform(markdown, None, key, platforms)
        assert text.strip(), key
        assert platform.fits(text), (key, platform.count(text))


@pytest.mark.parametrize("limit", [80, 140, 300])
def test_squeezed_char_limits_are_never_crossed(business_md, platforms, limit):
    """预算拧到多小都不越界（char_min 只作目标，不硬凑废话）。"""
    for key, platform in platforms.items():
        squeezed = replace(platform, char_limit=limit, char_min=1)
        text = render_for_platform(business_md, META, key, {key: squeezed})
        assert text.strip() and squeezed.count(text) <= limit, (key, limit, squeezed.count(text))


def test_custom_platforms_fall_back_to_the_generic_builder(tmp_path, business_md):
    path = write_platforms(tmp_path, f"""
platforms:
  blog_note:
    name: 博客短注
    language: zh
    char_limit: 300
    emoji: ✍️
    hashtags: [读书笔记]
    format: 短注
    title_rule: 直译标题
    notes: 兜底 builder
""")
    table = load_platforms(path)
    text = render_for_platform(business_md, META, "blog_note", table)
    assert "✍️" in text and "#读书笔记" in text
    assert CJK_RE.search(text) and re.search(r"[A-Za-z]{5,}", text)
    assert table["blog_note"].fits(text)


# ---------------------------------------------------------------- 命令行
def test_cli_list_prints_the_six_platforms(capsys):
    assert platforms_main(["--list"]) == 0
    printed = capsys.readouterr().out
    assert "共 6 个平台" in printed
    for key in EXPECTED_PLATFORMS:
        assert key in printed


def test_cli_renders_and_writes_files(tmp_path, capsys):
    out_dir = tmp_path / "posts"
    assert platforms_main(["--pack", str(FIXTURE), "--role", "business", "--platform", "weibo,x", "--out", str(out_dir)]) == 0
    table = load_platforms()
    for key in ("weibo", "x"):
        target = out_dir / f"{key}.md"
        assert target.is_file()
        assert table[key].fits(target.read_text(encoding="utf-8"))


def test_cli_reads_a_role_markdown_file(tmp_path, business_md, capsys):
    source = tmp_path / "business.md"
    source.write_text(business_md, encoding="utf-8")
    assert platforms_main(["--markdown", str(source), "--platform", "xiaohongshu"]) == 0
    assert "小红书" not in capsys.readouterr().out  # 输出的是文案本身，不是配置名


def test_cli_reports_bad_requests(capsys):
    assert platforms_main(["--pack", str(FIXTURE), "--platform", "douyin"]) == 1
    assert "没有这个平台" in capsys.readouterr().out
    assert platforms_main([]) == 1
    assert "需要 --pack" in capsys.readouterr().out

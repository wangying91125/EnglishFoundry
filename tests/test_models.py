"""foundry.models 数据模型测试（纯标准库，离线）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from time import struct_time

from foundry.models import Article, ContentPack, Story, as_aware_datetime, stable_id


def test_article_fills_id_and_utc_time():
    article = Article(
        source="BBC World",
        title="  Some Title  ",
        url="https://example.com/story",
        published="Tue, 27 Oct 2026 09:00:00 GMT",
        tags="news, tech",
    )
    assert article.id == stable_id("https://example.com/story")
    assert article.title == "Some Title"
    assert article.tags == ["news", "tech"]
    assert article.published == datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    assert article.published.tzinfo is not None


def test_stable_id_is_deterministic():
    assert stable_id("https://a", "x") == stable_id("https://a", "x")
    assert stable_id("https://a") != stable_id("https://b")
    assert len(stable_id("https://a")) == 16


def test_as_aware_datetime_accepts_common_shapes():
    expected = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    assert as_aware_datetime(expected) == expected
    assert as_aware_datetime(expected.replace(tzinfo=None)) == expected  # naive 按 UTC
    assert as_aware_datetime("2026-10-27T17:00:00+08:00") == expected  # 带偏移 → 转 UTC
    assert as_aware_datetime("2026-10-27T09:00:00Z") == expected
    assert as_aware_datetime(expected.timestamp()) == expected
    assert as_aware_datetime(struct_time((2026, 10, 27, 9, 0, 0, 0, 0, 0))) == expected
    assert as_aware_datetime("not a date") is None
    assert as_aware_datetime(None) is None
    assert as_aware_datetime("") is None


def test_article_roundtrip_and_preview():
    article = Article(
        id="abc",
        source="NPR",
        category="news",
        title="T",
        url="https://example.com/a",
        published=datetime(2026, 10, 27, 9, tzinfo=timezone.utc),
        content="word " * 100,
        tags=["a"],
    )
    payload = article.to_dict()
    assert payload["published"] == "2026-10-27T09:00:00+00:00"
    assert Article.from_dict(payload) == article
    assert json.dumps(payload, ensure_ascii=False)
    assert article.word_count == 100
    assert article.preview(20).endswith("…")


def test_story_aggregates_articles():
    early = Article(source="BBC", title="A", url="https://a", published="2026-10-27T06:00:00+00:00")
    late = Article(source="NPR", title="A", url="https://b", published="2026-10-27T08:00:00+00:00")
    story = Story(id="s1", category="news")
    story.add(late)
    story.add(early)
    assert story.title == "A" and story.url == "https://b"
    assert story.article_count == 2
    assert story.primary == early
    assert story.published == early.published
    assert story.sources == ["NPR", "BBC"]


def test_story_roundtrip():
    story = Story(
        id="s2",
        title="Headline",
        summary="摘要",
        url="https://example.com/s",
        source="BBC",
        category="tech",
        published=datetime(2026, 10, 27, tzinfo=timezone.utc),
        articles=[Article(source="BBC", title="Headline", url="https://example.com/s")],
    )
    assert Story.from_dict(story.to_dict()) == story


def test_content_pack_write_and_load(tmp_path):
    target = tmp_path / "output" / "packs" / "s3.json"
    pack = ContentPack(story_id="s3", data={"levels": {"B1": {"title": "T"}}, "vocabulary": [{"word": "gap"}]})
    written = pack.write(target)
    assert written == target
    assert json.loads(target.read_text(encoding="utf-8"))["story_id"] == "s3"

    loaded = ContentPack.load(target)
    assert loaded.story_id == "s3"
    assert loaded.data == pack.data
    assert loaded.path == target
    assert loaded.get("vocabulary") == [{"word": "gap"}]
    assert loaded.has("levels") and not loaded.has("missing")


def test_content_pack_empty_values_are_not_present(tmp_path):
    pack = ContentPack(story_id="s4", data={"quiz": [], "background_zh": "", "count": 0})
    assert not pack.has("quiz") and not pack.has("background_zh") and not pack.has("count")
    assert pack.keys() == ["quiz", "background_zh", "count"]

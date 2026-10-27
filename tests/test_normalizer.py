"""foundry.normalizer 测试：清洗、英文/时效过滤、去重与同事件聚合（全程离线）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from foundry import normalizer
from foundry.config import NormalizeSettings, load_settings
from foundry.models import Article, Story
from foundry.normalizer import (
    STOPWORDS,
    canonical_url,
    clean_html,
    cluster,
    english_ratio,
    keywords,
    normalize,
    normalize_title,
    prefilter,
    similarity,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

# 同一事件、不同来源的两篇报道（措辞不同，关键词高度重叠）
EVENT_TITLE_A = "Israel and Hamas agree to ceasefire deal in Gaza"
EVENT_TITLE_B = "Israel-Hamas ceasefire deal agreed in Gaza after months of talks"
UNRELATED_TITLE = "NASA launches new Artemis rocket for moon mission"

EVENT_BODY_A = (
    "The deal was announced early Tuesday. "
    "It pauses fighting and allows aid into Gaza. "
    "Mediators expect a second round of talks next week. "
)
EVENT_BODY_B = "The agreement ends months of fighting, officials said on Tuesday. " + "Aid convoys will enter Gaza on Friday. " * 4
UNRELATED_BODY = "Artemis lifted off at dawn carrying no crew but a full set of instruments. " * 4


def article(
    title: str,
    url: str,
    *,
    source: str = "Demo",
    hours_ago: int = 1,
    body: str = EVENT_BODY_A,
    category: str = "news",
    published: datetime | None | bool = False,
) -> Article:
    """构造一篇文章；``hours_ago`` 相对 NOW，``published=None`` 表示没有时间。"""
    if published is False:
        published = NOW - timedelta(hours=hours_ago)
    return Article(
        source=source,
        category=category,
        title=title,
        url=url,
        published=published,
        content=body,
        tags=["world"],
    )


def settings(**overrides) -> NormalizeSettings:
    values = {"max_age_hours": 24, "min_english_ratio": 0.6, "similarity": 0.55, "max_articles_per_story": 8}
    values.update(overrides)
    return NormalizeSettings(**values)


# ---------------------------------------------------------------- clean_html
def test_clean_html_removes_tags_entities_and_extra_whitespace():
    raw = "  Hello&nbsp;&amp;\r\n <b>world</b>   of\t\u200bnews  "
    assert clean_html(raw) == "Hello & world of news"


def test_clean_html_drops_non_content_tags():
    html = "<nav>Skip to content</nav><p>Real body text here.</p><script>var x = 1;</script><footer>Cookie banner</footer>"
    assert clean_html(html) == "Real body text here."


def test_clean_html_keeps_paragraph_breaks_when_asked():
    html = "<p>First paragraph.</p><div><p>Second   paragraph.</p></div>"
    assert clean_html(html, paragraphs=True) == "First paragraph.\n\nSecond paragraph."
    assert clean_html(html) == "First paragraph. Second paragraph."


def test_clean_html_unescapes_double_escaped_markup():
    assert clean_html("&lt;p&gt;Escaped &lt;b&gt;body&lt;/b&gt; text&lt;/p&gt;") == "Escaped body text"


def test_clean_html_survives_missing_bs4(monkeypatch):
    """没装 bs4 也要给出同样的结果（离线环境的兜底路径）。"""
    monkeypatch.setattr(normalizer, "BeautifulSoup", None)
    html = "<article><h1>Title</h1><p>Body one.</p><p>Body two.</p></article><script>noise()</script>"
    assert clean_html(html, paragraphs=True) == "Title\n\nBody one.\n\nBody two."
    assert clean_html(None) == "" and clean_html(123) == "123"


# ---------------------------------------------------------------- 语言与时间
def test_english_ratio_counts_letters_only():
    assert english_ratio("Plain English sentence") == 1.0
    assert english_ratio("Hello 世界 world") == pytest.approx(10 / 12)
    assert english_ratio("完全中文的内容") == 0.0
    assert english_ratio("12345 --- !!!") == 1.0  # 没有字母不判为“非英文”
    assert english_ratio(None) == 1.0


def test_prefilter_drops_non_english_articles():
    kept = prefilter(
        [
            article("Israeli team wins science prize", "https://en.example/1", body=EVENT_BODY_A),
            article("以色列团队获得科学奖", "https://cn.example/2", body="这是一篇纯中文的报道内容。" * 3),
        ],
        settings(),
        now=NOW,
    )
    assert [item.url for item in kept] == ["https://en.example/1"]


def test_prefilter_applies_recency_and_keeps_undated():
    stale = article("Old story about weather patterns", "https://a.example/old", hours_ago=48)
    fresh = article("Fresh story about weather patterns", "https://a.example/new", hours_ago=2)
    undated = article("Undated story about weather patterns", "https://a.example/x", published=None)

    kept = prefilter([stale, fresh, undated], settings(), now=NOW)
    assert [item.url for item in kept] == ["https://a.example/new", "https://a.example/x"]  # 新的在前，没时间也留
    assert [item.url for item in prefilter([stale], settings(max_age_hours=0), now=NOW)] == [stale.url]


# ---------------------------------------------------------------- 归一化键
def test_canonical_url_strips_tracking_fragment_and_default_port():
    assert (
        canonical_url("https://Example.COM:443/News//Story/?utm_source=tw&b=2&a=1#comments")
        == "https://example.com/News/Story?a=1&b=2"
    )
    assert canonical_url("//example.com/a") == "https://example.com/a"
    assert canonical_url("example.com/a/") == "https://example.com/a"
    assert canonical_url("http://example.com:80/a") == "http://example.com/a"
    assert canonical_url("https://example.com:8443/a") == "https://example.com:8443/a"
    assert canonical_url("https://example.com") == "https://example.com/"
    assert canonical_url("") == ""
    assert canonical_url("mailto:news@example.com") == ""
    # 路径大小写有含义，不能被压平
    assert canonical_url("https://example.com/A") != canonical_url("https://example.com/a")


def test_normalize_title_folds_case_punctuation_and_accents():
    assert normalize_title("Café  — Iran’s Deal: Costs &amp; Terms!!") == "cafe iran s deal costs terms"
    assert normalize_title("<b>Same  Title</b>") == normalize_title("same, title!")


def test_dedupe_removes_same_url_and_same_normalized_title():
    duplicates = [
        article("Ceasefire deal reached in Gaza region", "https://x.example/a", hours_ago=1),
        article("Ceasefire deal reached in Gaza region", "https://x.example/a?utm_campaign=newsletter", hours_ago=3),
        article("CEASEFIRE DEAL REACHED IN GAZA REGION!", "https://y.example/b", hours_ago=2),
    ]
    kept = prefilter(duplicates, settings(), now=NOW)
    assert [item.url for item in kept] == ["https://x.example/a"]
    assert kept[0].title == "Ceasefire deal reached in Gaza region"


# ---------------------------------------------------------------- 关键词与相似度
def test_keywords_drop_stopwords_and_stem_endings():
    tokens = keywords("The Hamas's strikes killed more than 20 people in Gaza City")
    assert "the" not in tokens and "more" not in tokens
    assert "20" not in tokens and "than" not in tokens
    assert "hamas" in tokens
    assert keywords("Strike strikes striking") == keywords("strike")
    assert all(stop not in keywords("Breaking news video: what you need to know today") for stop in STOPWORDS)


def test_similarity_is_jaccard_on_token_sets():
    assert similarity({"a", "b"}, {"a", "b"}) == 1.0
    assert similarity({"a", "b"}, {"c", "d"}) == 0.0
    assert similarity({"a", "b", "c"}, {"a", "b", "c", "d", "e"}) == pytest.approx(0.6)  # 3 / 5
    assert similarity(["a", "b"], ("b", "c", "d")) == pytest.approx(1 / 4)  # 可传任意可迭代
    assert similarity(set(), set()) == 0.0  # 没关键词不许合并
    same_event = similarity(keywords(EVENT_TITLE_A), keywords(EVENT_TITLE_B))
    assert same_event >= 0.55
    assert similarity(keywords(EVENT_TITLE_A), keywords(UNRELATED_TITLE)) == 0.0


def test_cluster_threshold_is_configurable():
    a = article(EVENT_TITLE_A, "https://one.example/a", source="BBC", body=EVENT_BODY_A)
    b = article(EVENT_TITLE_B, "https://two.example/b", source="Reuters", body=EVENT_BODY_B)
    assert len(cluster([a, b], settings(similarity=0.5))) == 1
    assert len(cluster([a, b], settings(similarity=0.95))) == 2


def test_cluster_respects_max_articles_per_story():
    copies = [
        article(f"Israel Hamas ceasefire deal agreed in Gaza zone {index}", f"https://r{index}.example/a", source=f"S{index}")
        for index in range(4)
    ]
    stories = cluster(copies[:1] + copies[1:], settings(max_articles_per_story=2))
    assert max(story.article_count for story in stories) <= 2
    assert sum(story.article_count for story in stories) == 4


def test_cluster_uses_newest_title_and_earliest_published():
    old = article(EVENT_TITLE_B, "https://two.example/b", source="Reuters", hours_ago=5, body=EVENT_BODY_B)
    new = article(EVENT_TITLE_A, "https://one.example/a", source="BBC", hours_ago=1, body=EVENT_BODY_A)
    story = cluster([old, new], settings())[0]
    assert story.title == EVENT_TITLE_A  # 最新那篇当标题
    assert story.published == old.published  # 最早那篇算事件时间
    assert story.primary is old  # 一手报道
    assert story.sources == ["BBC", "Reuters"]
    assert story.id and story.url == new.url


def test_cluster_never_merges_titles_without_keywords():
    first = article("UN", "https://a.example/1", body="Long English body text about something else entirely here.")
    second = article("!!!", "https://b.example/2", body="Another long English body text about a totally different topic.")
    assert len(cluster([first, second], settings())) == 2


# ---------------------------------------------------------------- normalize
def test_acceptance_two_sources_of_one_event_plus_unrelated_makes_two_stories():
    stories = normalize(
        [
            article(EVENT_TITLE_A, "https://bbc.example/a", source="BBC World", body=EVENT_BODY_A),
            article(EVENT_TITLE_B, "https://reuters.example/b", source="Reuters", body=EVENT_BODY_B),
            article(UNRELATED_TITLE, "https://nasa.example/c", source="NASA", body=UNRELATED_BODY, category="science"),
        ],
        settings(),
        now=NOW,
    )
    assert len(stories) == 2
    merged, single = stories
    assert merged.article_count == 2
    assert {item.source for item in merged.articles} == {"BBC World", "Reuters"}
    assert merged.category == "news" and single.category == "science"
    assert single.article_count == 1 and single.articles[0].source == "NASA"


def test_normalize_returns_stories_with_summary_and_clean_content():
    stories = normalize(
        [article(EVENT_TITLE_A, "https://a.example/1", body="<p>" + EVENT_BODY_A + "</p><script>noise</script>")],
        settings(),
        now=NOW,
    )
    story = stories[0]
    assert isinstance(story, Story)
    assert story.summary.startswith("The deal was announced early Tuesday.")
    assert "noise" not in story.summary and "<p>" not in story.articles[0].content
    payload = json.loads(json.dumps(story.to_dict(), ensure_ascii=False))
    assert payload["id"] and len(payload["articles"]) == 1


def test_normalize_accepts_settings_mapping_and_dict_articles():
    items = [
        article(EVENT_TITLE_A, "https://a.example/1", source="BBC", body=EVENT_BODY_A),
        article(EVENT_TITLE_B, "https://b.example/2", source="Reuters", body=EVENT_BODY_B),
    ]
    as_dicts = [item.to_dict() for item in items]
    assert len(normalize(as_dicts, settings(), now=NOW)) == 1
    assert len(normalize(items, {"similarity": 0.55}, now=NOW)) == 1
    project = load_settings()
    assert len(normalize(items, project, now=NOW)) == 1
    assert normalize([], settings(), now=NOW) == []
    with pytest.raises(TypeError):
        normalize(items, object())


def test_normalize_does_not_mutate_input_articles():
    original = article(EVENT_TITLE_A, "https://a.example/1", body="<p>Body   text</p>")
    before = original.to_dict()
    normalize([original], settings(), now=NOW)
    assert original.to_dict() == before


def test_resolve_settings_clamps_and_defaults():
    cfg = normalizer.resolve_settings({"min_english_ratio": 9, "similarity": -1, "max_age_hours": -5, "max_articles_per_story": 0})
    assert (cfg.min_english_ratio, cfg.similarity, cfg.max_age_hours, cfg.max_articles_per_story) == (1.0, 0.0, 0, 1)
    repo_default = normalizer.resolve_settings(None)
    assert repo_default.max_age_hours == 24
    duck = type("Bag", (), {"normalize": settings(similarity=0.7)})()
    assert normalizer.resolve_settings(duck).similarity == 0.7


def test_main_cli_reports_stories(tmp_path, capsys):
    """CLI 读 fetcher 写的 JSON，产物能落盘。"""
    payload = [
        article(EVENT_TITLE_A, "https://a.example/1", source="BBC", body=EVENT_BODY_A).to_dict(),
        article(EVENT_TITLE_B, "https://b.example/2", source="Reuters", body=EVENT_BODY_B).to_dict(),
        article(UNRELATED_TITLE, "https://c.example/3", source="NASA", body=UNRELATED_BODY).to_dict(),
    ]
    source = tmp_path / "articles.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    target = tmp_path / "stories.json"
    code = normalizer._main(["--input", str(source), "--hours", "0", "--save", str(target)])
    assert code == 0
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert len(saved) == 2
    assert {item["title"] for item in saved} == {EVENT_TITLE_A, UNRELATED_TITLE}
    printed = capsys.readouterr().out
    assert "2 个 Story" in printed and "BBC, Reuters" in printed


def test_main_cli_without_input_file_explains_next_step(tmp_path, capsys, monkeypatch):
    """还没有抓过数据时，告诉用户先跑 fetcher，而不是抛异常。"""
    monkeypatch.setattr(
        normalizer,
        "load_settings",
        lambda *args, **kwargs: SimpleNamespace(
            root=tmp_path,
            rel=lambda path: str(path),
            output=SimpleNamespace(raw=tmp_path / "raw"),
        ),
    )
    assert normalizer._main(["--input", str(tmp_path / "missing.json")]) == 1
    assert "foundry.fetcher" in capsys.readouterr().out

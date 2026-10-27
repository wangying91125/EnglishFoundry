"""foundry.fetcher 测试：全程离线（假响应 + 可关闭 feedparser），不访问真实网络。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from foundry import fetcher
from foundry.fetcher import (
    FetchError,
    RetryPolicy,
    RetryableFetchError,
    fetch_all,
    fetch_one,
    fetch_page_text,
    html_to_text,
    parse_feed,
)
from foundry.models import stable_id

FEED_URL = "https://demo.example/rss"
LONG_BODY = "Body paragraph with enough English words to count as an article. " * 12
SHORT_SUMMARY = "Just a one line summary."
PAGE_HTML = """<html><head><title>Story B</title><script>var noise = "SCRIPT_NOISE";</script></head>
<body>
<nav class="menu">Subscribe now to read more stories from the demo site every single day.</nav>
<article>
  <h1>Global item without text</h1>
  <p>Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar.</p>
  <p>Papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu one two three four five.</p>
  <p>Second paragraph is long enough to survive the block filter and reach the article body text.</p>
</article>
<aside>Related coverage and promotions must never land in the extracted body text of a story.</aside>
<footer>Cookie banner and copyright notice are here, definitely not part of the article content.</footer>
</body></html>"""

RSS_TWO_ITEMS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    "<channel><title>Demo Feed</title><link>https://demo.example</link>"
    "<item><title>Global item with full text</title><link>https://demo.example/a</link>"
    "<guid>https://demo.example/a</guid><pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate>"
    "<category>world</category><description>Intro line.</description>"
    f"<content:encoded><![CDATA[<p>{LONG_BODY}</p>]]></content:encoded></item>"
    "<item><title>Global item without text</title><link>https://demo.example/b</link>"
    "<pubDate>Tue, 27 Oct 2026 09:00:00 GMT</pubDate>"
    f"<description>{SHORT_SUMMARY}</description></item>"
    "</channel></rss>"
)

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Demo Atom</title>
  <entry>
    <title>Atom entry title</title>
    <link href="https://atom.example/1" rel="alternate"/>
    <id>tag:atom.example,2026:/1</id>
    <updated>2026-10-27T07:00:00Z</updated>
    <category term="tech"/>
    <content type="html">&lt;p&gt;Atom body text that is long enough to be used as the article body.&lt;/p&gt;</content>
  </entry>
</feed>"""

RDF_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item rdf:about="https://rdf.example/1">
    <title>RDF item</title>
    <link>https://rdf.example/1</link>
    <description>Short RDF summary.</description>
    <dc:date>2026-10-27T06:30:00Z</dc:date>
  </item>
</rdf:RDF>"""


def rss_with(items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Demo</title>{items}</channel></rss>"
    )


def item(title: str, link: str, *, hour: str = "08", body: str | None = LONG_BODY, summary: str = "") -> str:
    encoded = f"<content:encoded><![CDATA[<p>{body}</p>]]></content:encoded>" if body else ""
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>Tue, 27 Oct 2026 {hour}:00:00 GMT</pubDate>"
        f"<description>{summary}</description>{encoded}</item>"
    )


class FakeResponse:
    """只带 fetcher 会用到的那几个属性。"""

    def __init__(self, *, body: bytes | str = b"", status_code: int = 200, content_type: str = "application/xml"):
        self.raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    @property
    def content(self) -> bytes:
        return self.raw

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", "replace")


@pytest.fixture
def server(monkeypatch):
    """把 requests.get 换成路由表：URL -> 响应 / 可调用对象 / 异常。"""
    routes: dict[str, object] = {}
    calls: list[dict] = []
    fallback: list[object] = []

    def handler(url, **kwargs):
        calls.append({"url": url, **kwargs})
        route = routes.get(url, fallback[0] if fallback else None)
        if route is None:
            raise requests.ConnectionError(f"unrouted: {url}")
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(url, kwargs)
        return route

    monkeypatch.setattr(fetcher.requests, "get", handler)
    return SimpleNamespace(
        routes=routes,
        calls=calls,
        fallback=fallback,
        urls=lambda: [call["url"] for call in calls],
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """退避不真的睡（tenacity 不在时走内置循环）。"""
    monkeypatch.setattr(fetcher.time, "sleep", lambda *_args: None)


def source(**extra) -> dict:
    cfg = {"id": "demo", "name": "Demo", "category": "news", "type": "rss", "enabled": True, "url": FEED_URL}
    cfg.update(extra)
    cfg.setdefault("backoff", 0)
    return cfg


# ---------------------------------------------------------------- 正文来源
def test_feed_body_is_used_without_page_fetch(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    articles = fetch_one(source())
    assert [article.url for article in articles] == [
        "https://demo.example/b",  # 09:00 更新，排在前面
        "https://demo.example/a",
    ]
    assert articles[1].content.startswith("Body paragraph with enough")
    # 只有摘要那条才会去抓页面
    assert server.urls() == [FEED_URL, "https://demo.example/b", "https://demo.example/b", "https://demo.example/b"]


def test_article_identity_and_time_are_normalized(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://demo.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    article = next(a for a in fetch_one(source()) if a.url == "https://demo.example/a")
    assert article.id == stable_id("https://demo.example/a")
    assert article.published == datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    assert article.published.tzinfo is not None
    assert article.source == "Demo"
    assert article.category == "news"
    assert article.tags == ["world"]


def test_summary_only_entry_uses_page_body(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://demo.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    article = next(a for a in fetch_one(source()) if a.url == "https://demo.example/b")
    assert "Alpha bravo charlie" in article.content
    for noise in ("SCRIPT_NOISE", "Subscribe now", "Cookie banner", "Related coverage"):
        assert noise not in article.content


def test_failed_page_fetch_falls_back_to_summary(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)  # /b 没路由 -> 连接失败
    article = next(a for a in fetch_one(source()) if a.url == "https://demo.example/b")
    assert article.content == SHORT_SUMMARY
    assert article.word_count > 0


def test_fulltext_mode_never_and_always(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://demo.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    fetch_one(source(fulltext="never"))
    assert server.urls() == [FEED_URL]
    fetch_one(source(fulltext="always", min_fulltext_chars=100000))
    assert "https://demo.example/b" in server.urls()


def test_content_is_capped(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    article = next(a for a in fetch_one(source(max_content_chars=200)) if a.url == "https://demo.example/a")
    assert 0 < len(article.content) <= 200


# ---------------------------------------------------------------- 解析
def test_parse_feed_handles_rss_and_atom():
    assert [entry.title for entry in parse_feed(RSS_TWO_ITEMS.encode())] == [
        "Global item with full text",
        "Global item without text",
    ]
    entry = parse_feed(ATOM_FEED.encode())[0]
    assert entry.url == "https://atom.example/1"
    assert entry.tags == ("tech",)
    assert entry.published_at == datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)
    assert "Atom body text" in entry.body


def test_parse_feed_handles_rdf_and_ignores_html():
    entry = parse_feed(RDF_FEED.encode())[0]
    assert entry.url == "https://rdf.example/1"
    assert entry.published_at == datetime(2026, 10, 27, 6, 30, tzinfo=timezone.utc)
    assert parse_feed(b"<html><body>blocked</body></html>") == []


def test_parse_feed_works_without_feedparser(monkeypatch):
    monkeypatch.setattr(fetcher, "feedparser", None)
    assert parse_feed(RSS_TWO_ITEMS.encode())[0].title == "Global item with full text"


def test_html_to_text_strips_markup():
    assert html_to_text("<p>Hi <b>there</b> &amp; friends</p>") == "Hi there & friends"
    assert html_to_text("  plain   text ") == "plain text"
    assert html_to_text("") == ""


# ---------------------------------------------------------------- 重试与错误
def test_404_is_not_retried(server):
    server.routes[FEED_URL] = FakeResponse(body="nope", status_code=404)
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_one(source())
    assert server.urls() == [FEED_URL]


def test_5xx_is_retried_up_to_three_times(server):
    server.routes[FEED_URL] = FakeResponse(body="busy", status_code=503)
    with pytest.raises(RetryableFetchError, match="HTTP 503"):
        fetch_one(source())
    assert len(server.urls()) == 3


def test_retry_then_success(server):
    state = {"count": 0}

    def flaky(url, kwargs):
        state["count"] += 1
        if state["count"] < 3:
            raise requests.Timeout("slow")
        return FakeResponse(body=RSS_TWO_ITEMS)

    server.routes[FEED_URL] = flaky
    assert len(fetch_one(source(max_attempts=3))) == 2
    assert state["count"] == 3


def test_custom_user_agent_and_timeout_reach_the_wire(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    fetch_one(source(timeout=7, user_agent="FoundryTest/9"))
    assert server.calls[0]["headers"]["User-Agent"] == "FoundryTest/9"
    assert server.calls[0]["timeout"] == 7
    assert "rss" in server.calls[0]["headers"]["Accept"]


def test_fetch_page_text_never_raises(server):
    assert fetch_page_text("https://nowhere.example/x", RetryPolicy(max_attempts=2, backoff=0)) == ""
    server.routes["https://ok.example/p"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    assert "Alpha bravo charlie" in fetch_page_text("https://ok.example/p")


def test_source_without_url_is_rejected():
    with pytest.raises(FetchError, match="url"):
        fetch_one({"id": "broken"})


# ---------------------------------------------------------------- 取最新 / 去重
def test_limit_per_source_keeps_newest(server):
    feed = rss_with(
        item("Oldest", "https://demo.example/0", hour="07")
        + item("Middle", "https://demo.example/1", hour="08")
        + item("Newest", "https://demo.example/2", hour="09")
    )
    server.routes[FEED_URL] = FakeResponse(body=feed)
    assert [article.title for article in fetch_one(source(limit_per_source=2))] == ["Newest", "Middle"]


def test_duplicate_links_are_collapsed_within_a_source(server):
    feed = rss_with(item("Same", "https://demo.example/a") + item("Same again", "https://demo.example/a"))
    server.routes[FEED_URL] = FakeResponse(body=feed)
    assert len(fetch_one(source())) == 1


def test_items_without_link_or_text_are_skipped(server):
    feed = rss_with(
        "<item><title>No link</title><pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate></item>"
        + item("No text", "https://demo.example/empty", body=None)
        + item("Good", "https://demo.example/good")
    )
    server.routes[FEED_URL] = FakeResponse(body=feed)
    assert [article.title for article in fetch_one(source())] == ["Good"]


# ---------------------------------------------------------------- fetch_all
def test_one_broken_source_does_not_affect_others(server):
    good_feed = "https://good.example/feed"
    server.routes[good_feed] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://good.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    articles = fetch_all(
        sources=[
            {"id": "gone", "name": "Gone", "url": "https://gone.example/feed", "backoff": 0},
            {"id": "good", "name": "Good", "url": good_feed, "backoff": 0},
        ],
        limit_per_source=2,
    )
    assert {article.source for article in articles} == {"Good"}
    assert len(articles) == 2


def test_fetch_all_respects_enabled_flag(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://demo.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    articles = fetch_all(
        sources=[source(), {"id": "off", "name": "Off", "url": "https://off.example/feed", "enabled": False}],
        limit_per_source=1,
    )
    assert [article.source for article in articles] == ["Demo"]
    assert "https://off.example/feed" not in server.urls()


def test_fetch_all_deduplicates_shared_urls(server):
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://demo.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    mirror = {"id": "mirror", "name": "Mirror", "url": FEED_URL, "backoff": 0}
    articles = fetch_all(sources=[source(), mirror], limit_per_source=1)
    assert len(articles) == 1


def test_fetch_all_serial_matches_parallel_order(server):
    feeds = []
    for name, feed_url in (("One", "https://one.example/feed"), ("Two", "https://two.example/feed")):
        server.routes[feed_url] = FakeResponse(body=rss_with(item(f"{name} story", f"https://{name}.example/x")))
        feeds.append({"id": name.lower(), "name": name, "url": feed_url, "backoff": 0})
    serial = fetch_all(sources=feeds, limit_per_source=2, max_workers=1)
    server.calls.clear()
    parallel = fetch_all(sources=feeds, limit_per_source=2, max_workers=4)
    assert [article.source for article in serial] == ["One", "Two"]
    assert [article.source for article in parallel] == ["One", "Two"]
    assert [article.id for article in serial] == [article.id for article in parallel]
    assert sorted(server.urls()) == ["https://one.example/feed", "https://two.example/feed"]


def test_fetch_all_without_sources_returns_empty(server):
    assert fetch_all(sources=[]) == []
    assert server.calls == []


def test_cli_reports_counts_and_saves_json(tmp_path, server, capsys):
    """python -m foundry.fetcher 的端到端离线版：真清单 + 假传输，并写 JSON。"""
    feed = rss_with(item("CLI story", "https://cli.example/1"))
    server.fallback.append(FakeResponse(body=feed))  # 任何 URL 都回这份 feed
    target = tmp_path / "articles.json"
    assert fetcher._main(["--limit", "1", "--save", str(target)]) == 0
    out = capsys.readouterr().out
    assert "共 1 篇" in out  # 同一个 URL 被多个源转载，最后只留一篇
    assert "已写入" in out
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload[0]["title"] == "CLI story"
    assert payload[0]["published"].endswith("+00:00")
    assert payload[0]["id"] == stable_id("https://cli.example/1")


def test_repo_source_list_is_usable():
    """仓库里的真实清单能直接喂给 fetch_one：只检查参数装配，不发网络请求。"""
    from foundry.config import load_settings

    settings = load_settings()
    enabled = settings.enabled_sources()
    assert len(enabled) >= 10
    for cfg in enabled:
        policy = RetryPolicy.from_config(cfg, settings.fetch)
        assert policy.timeout > 0
        assert policy.user_agent.startswith("EnglishFoundry")
        assert policy.max_attempts == settings.fetch.max_attempts == 3


# ---------------------------------------------------------------- feedparser 主路径
def test_feedparser_entries_are_mapped(monkeypatch):
    """真机上 feedparser 是主解析器：用假 feedparser 验证字段映射（含 struct_time）。"""
    from time import struct_time

    parsed = {
        "bozo": 0,
        "entries": [
            {
                "title": "Mapped entry",
                "link": "https://fp.example/1",
                "summary": "Plain description.",
                "content": [
                    {"type": "text/html", "value": "<p>First half of the body.</p>"},
                    {"type": "text/html", "value": "<p>Second half of the body.</p>"},
                ],
                "tags": [{"term": "science"}, {"term": ""}, "not-a-dict"],
                "published_parsed": struct_time((2026, 10, 27, 5, 30, 0, 0, 0, 0)),
            },
            {"title": "No link at all"},
        ],
    }
    monkeypatch.setattr(fetcher, "feedparser", SimpleNamespace(parse=lambda payload: parsed))
    entries = parse_feed(b"<whatever/>")
    assert [entry.url for entry in entries] == ["https://fp.example/1", ""]  # 没链接的条目留给 _build_article 丢掉
    entry = entries[0]
    assert entry.title == "Mapped entry"
    assert entry.url == "https://fp.example/1"
    assert entry.summary == "Plain description."
    assert entry.body == "<p>First half of the body.</p>\n\n<p>Second half of the body.</p>"
    assert entry.tags == ("science",)
    assert entry.published_at == datetime(2026, 10, 27, 5, 30, tzinfo=timezone.utc)


def test_bozo_feed_falls_back_to_stdlib_parser(monkeypatch):
    monkeypatch.setattr(fetcher, "feedparser", SimpleNamespace(parse=lambda payload: {"bozo": 1, "entries": []}))
    assert len(parse_feed(RSS_TWO_ITEMS.encode())) == 2


def test_empty_feed_is_not_an_error(server):
    server.routes[FEED_URL] = FakeResponse(body=rss_with(""))
    assert fetch_one(source()) == []


def test_page_extraction_failure_still_keeps_summary(server, monkeypatch):
    """正文页面结构再离谱，也不能把条目丢掉。"""
    server.routes[FEED_URL] = FakeResponse(body=RSS_TWO_ITEMS)
    server.routes["https://demo.example/b"] = FakeResponse(body=PAGE_HTML, content_type="text/html")
    monkeypatch.setattr(fetcher, "_article_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("解析炸了")))
    article = next(a for a in fetch_one(source()) if a.url == "https://demo.example/b")
    assert article.content == SHORT_SUMMARY

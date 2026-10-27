"""按 ``config/sources.yaml`` 抓取全球英文 RSS/Atom 源，输出 ``List[Article]``。

分工与约定：
    * 源清单来自 :func:`foundry.config.load_settings`，每条已带 ``timeout`` / ``user_agent``
      等默认值；本模块只负责把原文取回来，时效过滤与聚类交给 normalizer。
    * 出网只有一个入口 :func:`_http_get`：自定义 User-Agent、显式超时、状态码检查；
      重试统一走 :func:`_with_retry`（tenacity 指数退避，默认最多 3 次）。
    * 正文优先取 feed 里的 ``content:encoded`` / Atom ``<content>``；只有摘要时再用
      requests + BeautifulSoup 去原文页面抓正文，抓不到就退回摘要（不丢条目）。
    * 单源失败只记日志（:func:`fetch_all` 逐源 try/except），单条失败也不影响整源；
      多个源用线程池并发抓取（默认 6 个），结果仍按源清单的顺序返回。
    * ``published`` 由 :func:`foundry.models.as_aware_datetime` 统一成 timezone-aware UTC，
      ``id`` 用 URL 的稳定哈希。
    * feedparser / tenacity / lxml 缺失时自动降级（标准库 XML 解析、内置退避循环、
      ``html.parser``），因此离线环境也能导入本模块并跑单测。全程不使用任何付费或
      需登录的 API，也不依赖 jina.ai 之类的第三方正文服务。

用法::

    from foundry.fetcher import fetch_all, fetch_one

    articles = fetch_all(limit_per_source=5)   # 全部启用的源
    articles = fetch_one({"id": "bbc_world", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"})

手工跑一次：``python -m foundry.fetcher --limit 3 --save``
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

import requests
from bs4 import BeautifulSoup

from foundry.config import DEFAULT_USER_AGENT, FetchSettings, Settings, load_settings
from foundry.models import Article, as_aware_datetime, stable_id

try:  # feedparser 是声明依赖；缺失时退化为标准库 XML 解析
    import feedparser
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    feedparser = None

try:  # tenacity 是声明依赖；缺失时退化为内置退避循环
    from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    Retrying = None

__all__ = [
    "FeedEntry",
    "FetchError",
    "RetryPolicy",
    "RetryableFetchError",
    "fetch_all",
    "fetch_one",
    "fetch_page_text",
    "html_to_text",
    "parse_feed",
]

log = logging.getLogger("foundry.fetcher")

T = TypeVar("T")

# ---- 出网默认参数（源配置可逐条覆盖）----
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF = 2.0
DEFAULT_WORKERS = 6

# ---- 正文长度策略 ----
MIN_FULLTEXT_CHARS = 320  # feed 正文短于此值 -> 去原文页面抓全文（BBC/NPR 这类只给摘要）
MIN_BLOCK_CHARS = 40  # 页面上短于 40 字符的块多为导航/按钮，丢掉
MAX_BLOCK_CHARS = 4000
MAX_CONTENT_CHARS = 20_000  # 单篇正文上限，避免把整页侧栏塞进 Article

FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
HTML_ACCEPT = "text/html, application/xhtml+xml;q=0.9, */*;q=0.5"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509})

# 页面上明显不是正文的标签
_STRIP_TAGS = (
    "aside",
    "button",
    "figcaption",
    "footer",
    "form",
    "header",
    "iframe",
    "nav",
    "noscript",
    "script",
    "style",
    "svg",
    "template",
)
_CONTENT_SELECTORS = (
    "article",
    "main",
    '[itemprop="articleBody"]',
    '[role="main"]',
    ".article-body",
    ".story-body__inner",
    "#story-body",
    ".entry-content",
    ".post-content",
    ".markdown-body",
)
_ENCODING_DECL = re.compile(rb"<\?xml[^>]*encoding=['\"][^'\"]+['\"]\s*\?>", re.IGNORECASE)


class FetchError(RuntimeError):
    """抓取彻底失败（不可重试，例如缺 url、4xx）。"""


class RetryableFetchError(FetchError):
    """值得重试的失败：超时、连接问题、5xx、429 等。"""


# 可重试的异常类型（requests / 标准库的传输层错误都算）
_RETRYABLE_TYPES: tuple[type[BaseException], ...] = (RetryableFetchError, requests.RequestException, OSError)


# ---------------------------------------------------------------- 配置小工具
def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 重试与出网
@dataclass(frozen=True)
class RetryPolicy:
    """一个源的出网参数：超时、UA、重试次数、退避基数。"""

    timeout: float = DEFAULT_TIMEOUT
    user_agent: str = DEFAULT_USER_AGENT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff: float = DEFAULT_BACKOFF

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], fetch: FetchSettings | None = None) -> "RetryPolicy":
        """源里写的值优先，其次 ``settings.fetch``，最后内置默认值。"""
        return cls(
            timeout=_as_float(cfg.get("timeout"), fetch.timeout if fetch else DEFAULT_TIMEOUT),
            user_agent=_as_text(cfg.get("user_agent"), fetch.user_agent if fetch else DEFAULT_USER_AGENT),
            max_attempts=_as_int(cfg.get("max_attempts"), fetch.max_attempts if fetch else DEFAULT_MAX_ATTEMPTS),
            backoff=_as_float(cfg.get("backoff"), fetch.backoff if fetch else DEFAULT_BACKOFF),
        )


def _is_retryable(error: BaseException) -> bool:
    return isinstance(error, _RETRYABLE_TYPES)


def _retry_reason(state: Any) -> str:
    """从 tenacity 的 RetryCallState 里取一句人能看懂的失败原因。"""
    outcome = getattr(state, "outcome", None)
    error = outcome.exception() if outcome is not None else None
    return f"{type(error).__name__}: {error}" if error else "未知错误"


def _with_retry(operation: Callable[[], T], *, attempts: int, backoff: float, label: str) -> T:
    """最多尝试 ``attempts`` 次，指数退避；装了 tenacity 就交给 tenacity。"""
    total = max(1, _as_int(attempts, DEFAULT_MAX_ATTEMPTS))
    if Retrying is not None:  # pragma: no cover - 取决于本机是否装了 tenacity
        retryer = Retrying(
            stop=stop_after_attempt(total),
            wait=wait_exponential(multiplier=backoff, max=max(backoff, backoff * 2 ** (total - 1))),
            retry=retry_if_exception_type(_RETRYABLE_TYPES),
            before_sleep=lambda state: log.warning(
                "%s 第 %s 次失败（%s 秒后重试）：%s",
                label,
                getattr(state, "attempt_number", "?"),
                getattr(state, "next_sleep_time", None) or 0,
                _retry_reason(state),
            ),
            reraise=True,
        )
        return retryer(operation)

    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - 只重试传输层/5xx，其余立刻抛出
            if attempt >= total or not _is_retryable(error):
                raise
            sleep_for = backoff * (2 ** (attempt - 1))
            log.warning("%s 第 %s 次失败（%.1f 秒后重试）：%s", label, attempt, sleep_for, error)
            if sleep_for > 0:
                time.sleep(sleep_for)


def _http_get(url: str, *, timeout: float, user_agent: str, accept: str = "*/*") -> requests.Response:
    """唯一的出网入口：带 UA / 超时 / 状态码检查（4xx 不重试，5xx 与 429 可重试）。"""
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
    status = _as_int(getattr(response, "status_code", 200), 200)
    if status >= 400:
        message = f"HTTP {status}：{url}"
        raise (RetryableFetchError(message) if status in RETRYABLE_STATUS else FetchError(message))
    return response


# ---------------------------------------------------------------- 正文提取
_parser_name: str | None = None


def _soup(markup: str) -> BeautifulSoup:
    """优先 lxml，没装就退到标准库 html.parser（探测结果缓存下来）。"""
    global _parser_name
    if _parser_name is None:
        try:
            BeautifulSoup("", "lxml")
            _parser_name = "lxml"
        except Exception:  # bs4.FeatureNotFound：lxml 未安装
            _parser_name = "html.parser"
    return BeautifulSoup(markup, _parser_name)


def html_to_text(markup: str) -> str:
    """HTML 片段 -> 一行纯文本（去标签、解实体、压缩空白）。"""
    text = markup if isinstance(markup, str) else _as_text(markup)
    if not text:
        return ""
    if "<" not in text:
        return " ".join(text.split())
    return " ".join(_soup(text).get_text(" ").split())


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("\n\n"), head.rfind(" "))
    return (head[:cut] if cut > limit // 2 else head).rstrip()


def _dedupe_blocks(texts: Iterable[str]) -> list[str]:
    """丢掉重复或被包含的块（``<li>`` 里套 ``<p>`` 会被各命中一次）。"""
    kept: list[str] = []
    for text in texts:
        if any(text == item or text in item for item in kept):
            continue
        kept = [item for item in kept if item not in text]
        kept.append(text)
    return kept


def _pick_container(soup: BeautifulSoup) -> BeautifulSoup:
    """挑一个最像正文的容器：段落数优先，其次文本长度。"""
    best: tuple[int, Any] | None = None
    for selector in _CONTENT_SELECTORS:
        for node in soup.select(selector):
            score = len(node.find_all(["p", "blockquote"])) * 1000 + len(node.get_text(" ").strip())
            if best is None or score > best[0]:
                best = (score, node)
    if best is not None and best[0] > 0:
        return best[1]
    return soup.body if soup.body is not None else soup


def _article_text(html: str, *, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """整页 HTML -> 正文纯文本（去导航/页脚，优先段落，取不到就退化到全文）。"""
    soup = _soup(html)
    for tag in soup.find_all(list(_STRIP_TAGS)):
        tag.decompose()
    container = _pick_container(soup)
    blocks: list[str] = []
    for block in container.find_all(["p", "blockquote", "li"]):
        text = " ".join(block.get_text(" ").split())
        if len(text) >= MIN_BLOCK_CHARS:
            blocks.append(text[:MAX_BLOCK_CHARS])
    body = "\n\n".join(_dedupe_blocks(blocks))
    if len(body) < MIN_FULLTEXT_CHARS:
        whole = " ".join(container.get_text(" ").split())
        if len(whole) > len(body):
            body = whole
    return _truncate(body, max_chars)


def fetch_page_text(url: str, policy: RetryPolicy | None = None) -> str:
    """抓原文页面并尽量只返回正文；失败返回空串（调用方退回摘要），绝不抛出。"""
    active = policy or RetryPolicy()
    try:
        html = _with_retry(
            lambda: _http_get(url, timeout=active.timeout, user_agent=active.user_agent, accept=HTML_ACCEPT).text,
            attempts=active.max_attempts,
            backoff=active.backoff,
            label=f"正文 {url}",
        )
    except Exception as error:  # noqa: BLE001 - 抓不到正文不算源失败
        log.info("正文抓取失败，退回 feed 内容：%s（%s）", url, error)
        return ""
    try:
        text = _article_text(html)
    except Exception as error:  # noqa: BLE001 - 页面结构再怪也不该丢掉条目
        log.info("正文解析失败，退回 feed 内容：%s（%s）", url, error)
        return ""
    if not text:
        log.debug("页面没有可用正文：%s", url)
    return text


# ---------------------------------------------------------------- feed 解析
@dataclass
class FeedEntry:
    """一条 RSS/Atom 条目（feedparser 与标准库解析都归一到这个结构）。"""

    title: str = ""
    url: str = ""
    summary: str = ""
    body: str = ""
    tags: tuple[str, ...] = ()
    published: Any = None  # struct_time / str / datetime，交给 models.as_aware_datetime

    @property
    def published_at(self) -> datetime | None:
        return as_aware_datetime(self.published)


def _first_url(*candidates: Any) -> str:
    for candidate in candidates:
        text = _as_text(candidate)
        if text.lower().startswith(("http://", "https://")):
            return text
    return ""


def _from_feedparser(entry: Mapping[str, Any]) -> FeedEntry:
    parts = [_as_text(item.get("value")) for item in entry.get("content") or [] if isinstance(item, Mapping)]
    tags = tuple(
        _as_text(tag.get("term"))
        for tag in entry.get("tags") or []
        if isinstance(tag, Mapping) and _as_text(tag.get("term"))
    )
    return FeedEntry(
        title=_as_text(entry.get("title")),
        url=_first_url(entry.get("link"), entry.get("href"), entry.get("id")),
        summary=_as_text(entry.get("summary") or entry.get("description")),
        body="\n\n".join(part for part in parts if part),
        tags=tags,
        published=(
            entry.get("published_parsed")
            or entry.get("updated_parsed")
            or entry.get("published")
            or entry.get("updated")
            or entry.get("date")
        ),
    )


def _localname(tag: Any) -> str:
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1].lower()


def _xml_root(payload: bytes | str) -> ET.Element | None:
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload or b"")
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        try:  # 编码声明和实际字节打架时，去掉声明再试一次
            return ET.fromstring(_ENCODING_DECL.sub(b"", raw))
        except ET.ParseError as error:
            log.debug("XML 解析失败（可能不是 feed）：%s", error)
            return None


def _entry_from_xml(node: ET.Element) -> FeedEntry:
    texts: dict[str, list[str]] = {}
    links: list[tuple[str, str]] = []
    tags: list[str] = []
    for child in node:
        name = _localname(child.tag)
        value = " ".join("".join(child.itertext()).split())
        if name == "link":
            href = _as_text(child.get("href"))
            if href:
                links.append((_as_text(child.get("rel"), "alternate"), href))
            if value:
                texts.setdefault("link", []).append(value)
        elif name == "category":
            term = _as_text(child.get("term")) or value
            if term:
                tags.append(term)
        elif value:
            texts.setdefault(name, []).append(value)

    def first(name: str) -> str:
        return (texts.get(name) or [""])[0]

    alternate = next((href for rel, href in links if rel == "alternate"), "")
    body = "\n\n".join(texts.get("encoded") or texts.get("content") or texts.get("body") or [])
    return FeedEntry(
        title=" ".join(first("title").split()),
        url=_first_url(first("link"), alternate, first("id"), *(href for _, href in links)),
        summary=first("summary") or first("description"),
        body=body,
        tags=tuple(tags),
        published=first("published") or first("pubdate") or first("updated") or first("modified") or first("date"),
    )


def _from_xml(payload: bytes | str) -> list[FeedEntry]:
    """标准库兜底解析：RSS 2.0 / Atom / RDF 的 ``item`` / ``entry`` 都吃。"""
    root = _xml_root(payload)
    if root is None:
        return []
    entries = [_entry_from_xml(node) for node in root.iter() if _localname(node.tag) in ("item", "entry")]
    return [entry for entry in entries if entry.url or entry.title]


def parse_feed(payload: bytes | str) -> list[FeedEntry]:
    """把 feed 内容解析成条目列表（feedparser 优先，标准库兜底）。"""
    if feedparser is not None:  # pragma: no cover - 取决于本机是否装了 feedparser
        try:
            parsed = feedparser.parse(payload)
        except Exception as error:  # noqa: BLE001 - 坏字节不该让整源挂掉
            log.debug("feedparser 异常，改用标准库解析：%s", error)
        else:
            raw = [item for item in parsed.get("entries") or [] if isinstance(item, Mapping)]
            entries = [entry for entry in map(_from_feedparser, raw) if entry.url or entry.title]
            if entries or not parsed.get("bozo"):
                return entries
            log.debug("feedparser 没解析出条目，改用标准库解析")
    return _from_xml(payload)


def _newest_first(entries: Sequence[FeedEntry]) -> list[FeedEntry]:
    """按发布时间从新到旧；没时间戳的保持 feed 原顺序并排在后面。"""
    timed = [(entry.published_at, index, entry) for index, entry in enumerate(entries) if entry.published_at]
    undated = [entry for entry in entries if not entry.published_at]
    timed.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [entry for _, _, entry in timed] + undated


# ---------------------------------------------------------------- 组装 Article
def _needs_fulltext(mode: str, text: str, threshold: int) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    return len(text) < threshold  # auto：只有摘要/正文太短才去抓页面


def _make_page_fetcher(policy: RetryPolicy) -> Callable[[str], str]:
    """把出网参数绑到正文抓取上，测试时也可以直接换一个回调。"""

    def fetch(link: str) -> str:
        return fetch_page_text(link, policy)

    return fetch


def _build_article(
    entry: FeedEntry,
    cfg: Mapping[str, Any],
    *,
    page_fetcher: Callable[[str], str] = fetch_page_text,
) -> Article | None:
    """一条条目 -> 一篇 Article；没有链接或没有任何文字时返回 None。"""
    if not entry.url:
        log.debug("跳过没有链接的条目：%s", entry.title[:60])
        return None
    body = html_to_text(entry.body)
    summary = html_to_text(entry.summary)
    text = max((body, summary), key=len)
    mode = _as_text(cfg.get("fulltext"), "auto")
    if _needs_fulltext(mode, text, _as_int(cfg.get("min_fulltext_chars"), MIN_FULLTEXT_CHARS)):
        page = _as_text(page_fetcher(entry.url))
        if len(page) > len(text):
            text = page
    if not text:
        log.debug("跳过没有正文的条目：%s", entry.url)
        return None
    return Article(
        id=stable_id(entry.url),
        source=_as_text(cfg.get("name"), _as_text(cfg.get("id"))),
        category=_as_text(cfg.get("category"), "news"),
        title=entry.title,
        url=entry.url,
        published=entry.published_at,
        content=_truncate(text, _as_int(cfg.get("max_content_chars"), MAX_CONTENT_CHARS)),
        tags=list(entry.tags),
    )


def _resolve_limit(cfg: Mapping[str, Any]) -> int | None:
    """``limit_per_source`` 归一：None / 空 / 非正数 = 不限制。"""
    if cfg.get("limit_per_source") in (None, ""):
        return None
    value = _as_int(cfg.get("limit_per_source"), 0)
    return value if value > 0 else None


# ---------------------------------------------------------------- 对外接口
def fetch_one(source_cfg: Mapping[str, Any], *, settings: Settings | None = None) -> list[Article]:
    """抓取单个源，返回该源的 Article 列表（条目级失败只记日志）。

    ``source_cfg`` 就是 :meth:`foundry.config.Settings.enabled_sources` 里的一个字典；除
    ``url`` / ``timeout`` / ``user_agent`` 外还支持这些可选键：``fulltext``
    （``auto`` / ``always`` / ``never``）、``min_fulltext_chars``、``max_content_chars``、
    ``limit_per_source``（取最新的 N 条）。

    Raises:
        FetchError: 源没有 ``url``，或 feed 重试后仍取不回来（4xx 直接失败）。
    """
    cfg = dict(source_cfg or {})
    url = _as_text(cfg.get("url"))
    if not url:
        raise FetchError(f"源缺少 url：{cfg.get('id') or cfg}")
    label = _as_text(cfg.get("name"), _as_text(cfg.get("id"), url))
    policy = RetryPolicy.from_config(cfg, settings.fetch if settings else None)

    payload = _with_retry(
        lambda: _http_get(url, timeout=policy.timeout, user_agent=policy.user_agent, accept=FEED_ACCEPT).content,
        attempts=policy.max_attempts,
        backoff=policy.backoff,
        label=f"feed {label}",
    )
    entries = parse_feed(payload)
    ordered = _newest_first(entries)
    limit = _resolve_limit(cfg)
    selected = ordered[:limit] if limit else ordered
    page_fetcher = _make_page_fetcher(policy)

    articles: list[Article] = []
    seen: set[str] = set()
    for entry in selected:
        try:
            article = _build_article(entry, cfg, page_fetcher=page_fetcher)
        except Exception as error:  # noqa: BLE001 - 一条坏条目不拖垮整源
            log.debug("条目处理失败：%s（%s）", entry.url, error)
            continue
        if article is None or article.id in seen:
            continue
        seen.add(article.id)
        articles.append(article)

    if articles:
        log.info("源 %s：%s/%s 条可用，平均 %s 词", label, len(articles), len(entries), _avg_words(articles))
    else:
        log.warning("源 %s 没有取到可用条目（feed 条目 %s 条）", label, len(entries))
    return articles


def fetch_all(
    enabled_only: bool = True,
    limit_per_source: int | None = None,
    *,
    settings: Settings | None = None,
    sources: Sequence[Mapping[str, Any]] | None = None,
    max_workers: int | None = None,
) -> list[Article]:
    """抓取清单里的所有源，返回扁平的 ``List[Article]``。

    Args:
        enabled_only: 只抓 ``enabled: true`` 的源（默认）。
        limit_per_source: 每个源最多取几条（按时间取最新的）；None 用配置里的
            ``fetch.limit_per_source``，0 或负数表示不限制。
        settings: 复用已加载的配置；None 时调用 :func:`foundry.config.load_settings`。
        sources: 直接指定源清单（测试或临时只抓几个源时用）。
        max_workers: 并发线程数，默认 6；传 1 表示串行。

    Returns:
        按源顺序、源内时间从新到旧排列的 Article；单源失败只记日志、不影响其他源。
    """
    settings = settings or load_settings()
    if sources is None:
        sources = settings.enabled_sources() if enabled_only else settings.all_sources()
    limit = settings.fetch.limit_per_source if limit_per_source is None else limit_per_source
    if limit is not None and _as_int(limit, 0) <= 0:
        limit = None

    queue: list[dict[str, Any]] = []
    for item in sources or ():
        cfg = dict(item or {})
        if enabled_only and not cfg.get("enabled", True):
            continue
        cfg["limit_per_source"] = limit
        queue.append(cfg)
    if not queue:
        log.warning("没有可抓取的源（enabled_only=%s）", enabled_only)
        return []

    workers = max(1, min(_as_int(max_workers, DEFAULT_WORKERS) or 1, len(queue)))
    failures: list[str] = []

    def run(index: int, cfg: dict[str, Any]) -> tuple[int, list[Article]]:
        label = _as_text(cfg.get("name"), _as_text(cfg.get("id"), _as_text(cfg.get("url"))))
        try:
            return index, fetch_one(cfg, settings=settings)
        except Exception as error:  # noqa: BLE001 - 单源失败不能带走其他源
            log.warning("源 %s 抓取失败：%s", label, error)
            failures.append(label)
            return index, []

    if workers == 1:
        filled = [run(index, cfg) for index, cfg in enumerate(queue)]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
            filled = list(pool.map(lambda pair: run(*pair), list(enumerate(queue))))

    results: list[list[Article]] = [[] for _ in queue]
    for index, part in filled:
        results[index] = part

    collected: list[Article] = []
    seen: set[str] = set()
    for part in results:
        for article in part:
            if article.id in seen:  # 同一 URL 被多个源转载
                continue
            seen.add(article.id)
            collected.append(article)
    log.info(
        "抓取完成：%s 个源 / %s 篇（失败 %s 个源：%s）",
        len(queue),
        len(collected),
        len(failures),
        ", ".join(failures) or "-",
    )
    return collected


def _avg_words(articles: Sequence[Article]) -> int:
    if not articles:
        return 0
    return sum(article.word_count for article in articles) // len(articles)


def _main(argv: Sequence[str] | None = None) -> int:
    """命令行跑一次抓取，方便排障：``python -m foundry.fetcher --limit 3``"""
    parser = argparse.ArgumentParser(description="抓取 config/sources.yaml 里的英文 RSS/Atom 源")
    parser.add_argument("--limit", type=int, default=None, help="每个源最多取几条（默认用配置值）")
    parser.add_argument("--include-disabled", action="store_true", help="连 enabled: false 的源一起抓")
    parser.add_argument("--workers", type=int, default=None, help="并发线程数，1 为串行")
    parser.add_argument("--save", nargs="?", const="", metavar="PATH", help="结果写 JSON（默认 output/raw/articles.json）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    articles = fetch_all(
        enabled_only=not args.include_disabled,
        limit_per_source=args.limit,
        settings=settings,
        max_workers=args.workers,
    )
    counts = Counter(article.source for article in articles)
    print(f"共 {len(articles)} 篇，来自 {len(counts)} 个源")
    for source, count in counts.most_common():
        print(f"  {source}: {count}")

    if args.save is not None:
        target = Path(args.save) if args.save else settings.output.raw / "articles.json"
        if not target.is_absolute():
            target = (settings.root / target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = [article.to_dict() for article in articles]
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {settings.rel(target)}")
    return 0 if articles else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

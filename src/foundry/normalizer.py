"""``Article`` → ``Story``：清洗、英文/时效过滤、去重与同事件聚合。

分工与约定：
    * 输入是 :mod:`foundry.fetcher` 抓回来的 ``List[Article]``（也接受 dict / JSON 反序列化
      出来的映射），输出 ``List[Story]``；本模块不联网、不调 LLM，纯离线可跑。
    * :func:`clean_html` 负责去标签、解实体、统一空白，标题里残留的 ``&amp;`` / ``&nbsp;``
      一并清掉；装了 bs4 就用它，没装则正则兜底（结果一致：正文按块分段）。
    * 只保留英文：看字母字符里 ASCII 字母的占比（默认 ≥ 0.6），标题和正文一起算；
      完全没有字母的内容（纯数字/纯符号）不算“非英文”，只有标题和正文都为空时才丢。
    * 时效：默认只留最近 ``normalize.max_age_hours`` 小时（24h）；``published`` 缺失的
      条目无法判断新旧，保留（与 fetcher“不丢条目”的口径一致）；配 0 或负数关闭过滤。
    * 去重：URL 规范化（协议/主机小写、去 fragment、去 utm 之类跟踪参数、去默认端口）
      与标题归一化（小写、去音标、去标点、压缩空白）两个键，任意命中即重复，保留最新一篇。
    * 聚类：标题去掉停用词、轻度还原词形得到 token 集合，与每个已有 Story 的成员做
      single-link Jaccard，最高分 ≥ ``normalize.similarity``（默认 0.55）就并入，否则新建；
      单个 Story 最多并 ``normalize.max_articles_per_story`` 篇。
    * 顺序：过滤后按时间从新到旧入聚类，所以 ``Story.title`` 取最新那篇的标题，而
      ``Story.primary`` / ``Story.published`` 仍是时间最早的一手报道；产出按“来源数多 →
      时间新”排列。
    * 阈值只来自 :class:`foundry.config.NormalizeSettings`；``normalize(articles, settings)``
      的 settings 可传 ``Settings`` / ``NormalizeSettings`` / 映射 / None（None 时读配置）。

用法::

    from foundry.config import load_settings
    from foundry.normalizer import normalize

    stories = normalize(fetch_all(), load_settings())

手工跑一次：``python -m foundry.normalizer --hours 24``（默认读 output/raw/articles.json）
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from foundry.config import NormalizeSettings, Settings, load_settings
from foundry.models import Article, Story, stable_id

try:  # bs4 是声明依赖；缺失时退化为正则去标签，保证离线也能导入本模块
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    BeautifulSoup = None

__all__ = [
    "STOPWORDS",
    "canonical_url",
    "clean_html",
    "cluster",
    "english_ratio",
    "keywords",
    "normalize",
    "normalize_title",
    "prefilter",
    "similarity",
]

log = logging.getLogger("foundry.normalizer")

SUMMARY_CHARS = 320  # Story.summary 的软上限
SUMMARY_SENTENCES = 3

# 明显不是正文的标签（bs4 直接 decompose，正则兜底整段删掉）
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "button",
    "figcaption",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
)
_BLOCK_TAGS = ("p", "li", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "td", "th")

_SCRIPTISH = re.compile(r"(?is)<(%s)\b.*?</\1\s*>" % "|".join(_DROP_TAGS))
_BLOCK_END = re.compile(r"(?i)<\s*(?:br\s*/?|/(?:p|div|li|h[1-6]|tr|ul|ol|blockquote|section|article))\s*>")
_TAG = re.compile(r"(?s)<[^>]*>")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_WORD = re.compile(r"[a-z0-9]+(?:['\-][a-z0-9]+)*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_TRACKING_KEYS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid", "vero_conv", "pk_campaign", "pk_source"}
)

# 曲线引号/破折号等统一成 ASCII，标题归一化与 token 化都更稳
_PUNCT_MAP = str.maketrans(
    {
        "\u00a0": " ",  # nbsp
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
    }
)

# 英文虚词 + 源里高频栏目词（“BREAKING”“Watch”“video”），它们不代表事件本身
STOPWORDS = frozenset(
    """
a about above after again against all also am an and any are as at be because been before being below between
both but by can cannot could did do does doing down during each few for from further had has have having he her
here hers him his how i if in into is it its itself just like me more most much must my no nor not now of off on
once only or other our ours out over own same so some such than that the their them then there these they this
those through to too under until up upon us very was we were what when where which while who whom why will with
would you your yours
said says say tell told tells report reported breaking live update updates watch listen video photo photos podcast
newsletter opinion analysis exclusive excerpt transcript story stories headline headlines article
amp
""".split()
)


# ---------------------------------------------------------------- 小工具
def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _timestamp(moment: datetime | None) -> float:
    """排序用：没有时间 = 最旧。"""
    return moment.timestamp() if isinstance(moment, datetime) else float("-inf")


# ---------------------------------------------------------------- HTML 清洗
def _regex_text(markup: str) -> str:
    """没有 bs4 时的兜底：整段脚本删掉，块边界换成换行，其余标签删掉。"""
    text = _SCRIPTISH.sub(" ", markup)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub(" ", text)
    return html_lib.unescape(text)


def _soup_text(markup: str) -> str:
    """bs4 路径：丢掉非正文标签，按块取文本（块之间用换行分隔）。"""
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    blocks: list[str] = []
    for node in soup.find_all(list(_BLOCK_TAGS)):
        chunk = " ".join(node.get_text(" ").split())
        if chunk:
            blocks.append(chunk)
    inline = " ".join(soup.get_text("\n").split())
    if not blocks or len(inline) > 1.6 * sum(len(chunk) for chunk in blocks):
        # 没有明显的块结构（或绝大部分文本都在块外）时，整段取出来更安全
        return inline
    return "\n".join(_dedupe_blocks(blocks))


def _dedupe_blocks(blocks: Sequence[str]) -> list[str]:
    """丢掉重复或被包含的块（``<li>`` 里套 ``<p>`` 会被各命中一次）。"""
    kept: list[str] = []
    for block in blocks:
        if any(block == item or block in item for item in kept):
            continue
        kept = [item for item in kept if item not in block]
        kept.append(block)
    return kept


def _tidy(text: str, *, paragraphs: bool = False) -> str:
    """统一空白与易混淆标点：控制字符丢掉，每行压成一行。"""
    text = text.translate(_PUNCT_MAP)
    text = "".join(ch for ch in text if ch in "\n\r" or not unicodedata.category(ch).startswith("C"))
    text = text.replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    kept = [line for line in lines if line]
    if not kept:
        return ""
    return "\n\n".join(kept) if paragraphs else " ".join(kept)


def clean_html(markup: Any, *, paragraphs: bool = False) -> str:
    """HTML/转义文本 -> 干净纯文本（去标签、解实体、统一空白）。

    Args:
        markup: 原始字符串（非字符串按空串处理，None 不报错）。
        paragraphs: True 时保留段落分隔（``\\n\\n``），用于正文；False 压成一行，用于标题。
    """
    text = markup if isinstance(markup, str) else _as_text(markup)
    if not text:
        return ""
    cleaned = _soup_text(text) if BeautifulSoup is not None else _regex_text(text)
    if "<" in cleaned:  # feed 里正文常被二次转义（&lt;p&gt;），解出来的假标签再清一遍
        cleaned = _TAG.sub(" ", html_lib.unescape(cleaned))
    return _tidy(cleaned, paragraphs=paragraphs)


# ---------------------------------------------------------------- 语言与时效
def english_ratio(text: Any) -> float:
    """字母字符里 ASCII 英文字母的占比（0.0~1.0）；没有字母视为 1.0。"""
    letters = [ch for ch in _as_text(text) if ch.isalpha()]
    if not letters:
        return 1.0
    english = sum(1 for ch in letters if ch.isascii())
    return english / len(letters)


def _is_english(article: Article, min_ratio: float) -> bool:
    if min_ratio <= 0:
        return True
    return english_ratio(f"{article.title} {article.content}") >= min_ratio


def _is_fresh(article: Article, max_age_hours: int, now: datetime) -> bool:
    if max_age_hours <= 0 or article.published is None:
        return True
    return now - article.published <= timedelta(hours=max_age_hours)


# ---------------------------------------------------------------- 归一化键
def canonical_url(url: Any) -> str:
    """URL 规范化：只处理 http(s)，主机小写、去 fragment/跟踪参数/默认端口、路径去尾斜杠。

    非网页链接（mailto、缺主机等）返回空串，调用方退回其它去重键。
    """
    raw = _as_text(url)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif not _SCHEME.match(raw):
        raw = f"https://{raw.lstrip('/')}"
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    try:
        port = parts.port
    except ValueError:  # 端口写成非法值，当没有端口处理
        port = None
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(pairs)), ""))


def normalize_title(title: Any) -> str:
    """标题归一化（去重键）：去标签、去音标、小写、去标点、压缩空白。"""
    text = unicodedata.normalize("NFKD", clean_html(title))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return " ".join(re.sub(r"[^a-z0-9\s]+", " ", text).split())


# ---------------------------------------------------------------- 关键词与相似度
def _drop_final_e(token: str) -> str:
    """词尾 e 一律去掉（agree -> agre），让 agree/agreed/agrees 能落到同一个词根。"""
    return token[:-1] if len(token) > 3 and token.endswith("e") else token


def _stem(token: str) -> str:
    """极轻量的词形还原：把复数/进行时/过去式收到同一个词根，两端规则完全一致。

    只求确定性（strike/strikes/striking -> strik，agree/agreed/agrees -> agre），
    不做词典级例外映射；不规则动词（made/make）碰不上就不碰。
    """
    token = _drop_final_e(token)
    if len(token) > 4 and token.endswith(("ies", "ied")):  # studies / studied -> study
        token = token[:-3] + "y"
    for suffix in ("ings", "ing", "edly", "ed", "es", "s"):
        if not token.endswith(suffix) or len(token) - len(suffix) < 3:
            continue
        if suffix == "s" and token[-2] in "aeiouy":  # hamas / virus 这类结尾不是复数
            continue
        token = token[: -len(suffix)]
        break
    return _drop_final_e(token)


def keywords(text: Any) -> set[str]:
    """从文本（一般是标题）提取关键词集合：去停用词、去纯数字、还原词形。"""
    normalized = normalize_title(text)
    tokens: set[str] = set()
    for word in _WORD.findall(normalized):
        if len(word) < 2 or word.isdigit() or word in STOPWORDS:
            continue
        tokens.add(_stem(word))
    return tokens


def similarity(left: Iterable[str], right: Iterable[str]) -> float:
    """Jaccard 相似度 |A∩B| / |A∪B|；任意一边为空 -> 0.0（没有关键词就不许合并）。"""
    a, b = set(left or ()), set(right or ())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------- 配置
def resolve_settings(settings: Settings | NormalizeSettings | Mapping[str, Any] | None = None) -> NormalizeSettings:
    """把 ``Settings`` / ``NormalizeSettings`` / 映射 / None 统一成可用的阈值（并做夹取）。"""
    if isinstance(settings, Settings):
        cfg = settings.normalize
    elif isinstance(settings, Mapping):
        cfg = NormalizeSettings.from_config(settings)
    elif isinstance(settings, NormalizeSettings):
        cfg = settings
    elif settings is None:
        cfg = load_settings().normalize
    elif isinstance(getattr(settings, "normalize", None), NormalizeSettings):
        cfg = settings.normalize  # duck typing：只带 .normalize 的对象也认
    else:
        raise TypeError(f"normalize 需要 Settings / NormalizeSettings / 映射，收到 {type(settings).__name__}")
    return NormalizeSettings(
        max_age_hours=max(0, cfg.max_age_hours),
        min_english_ratio=min(1.0, max(0.0, cfg.min_english_ratio)),
        similarity=min(1.0, max(0.0, cfg.similarity)),
        max_articles_per_story=max(1, cfg.max_articles_per_story),
    )


# ---------------------------------------------------------------- 清洗 + 过滤 + 去重
def _coerce(article: Any) -> Article | None:
    if isinstance(article, Article):
        return article
    if isinstance(article, Mapping):
        return Article.from_dict(article)
    return None


def _tidied(article: Article) -> Article:
    """返回清洗过标题/正文的副本（不改动调用方传进来的对象）。"""
    tags: list[str] = []
    for tag in article.tags:
        text = clean_html(tag)
        if text and text not in tags:
            tags.append(text)
    return replace(
        article,
        title=clean_html(article.title),
        content=clean_html(article.content, paragraphs=True),
        tags=tags,
    )


def prefilter(
    articles: Iterable[Any],
    settings: Settings | NormalizeSettings | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> list[Article]:
    """清洗 -> 英文过滤 -> 时效过滤 -> 去重，返回时间从新到旧的 Article。"""
    cfg = resolve_settings(settings)
    moment = now.astimezone(timezone.utc) if isinstance(now, datetime) else datetime.now(timezone.utc)
    stats: Counter[str] = Counter()
    total = 0

    candidates: list[Article] = []
    for raw in articles or ():
        total += 1
        article = _coerce(raw)
        if article is None:
            stats["invalid"] += 1
            continue
        cleaned = _tidied(article)
        if not (cleaned.title or cleaned.content):
            stats["empty"] += 1
            continue
        if not _is_english(cleaned, cfg.min_english_ratio):
            stats["non_english"] += 1
            log.debug("非英文（占比 %.2f）丢弃：%s", english_ratio(f"{cleaned.title} {cleaned.content}"), cleaned.title)
            continue
        if not _is_fresh(cleaned, cfg.max_age_hours, moment):
            stats["stale"] += 1
            continue
        candidates.append(cleaned)

    candidates.sort(key=lambda item: _timestamp(item.published), reverse=True)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    kept: list[Article] = []
    for article in candidates:
        url_key = canonical_url(article.url)
        title_key = normalize_title(article.title)
        if (url_key and url_key in seen_urls) or (title_key and title_key in seen_titles):
            stats["duplicate"] += 1
            continue
        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        kept.append(article)

    dropped = ", ".join(
        f"{label} {stats[key]}"
        for label, key in (
            ("非英文", "non_english"),
            ("超时效", "stale"),
            ("重复", "duplicate"),
            ("空内容", "empty"),
            ("无法识别", "invalid"),
        )
        if stats[key]
    )
    log.info(
        "清洗完成：输入 %s 篇 -> 保留 %s 篇（丢弃：%s）",
        total,
        len(kept),
        dropped or "无",
    )
    return kept


# ---------------------------------------------------------------- 聚类
def _new_story(article: Article) -> Story:
    """用第一篇（最新的一篇）做种子建 Story，id 稳定取 URL/标题。"""
    return Story(
        id=stable_id(canonical_url(article.url) or normalize_title(article.title) or article.title),
        title=article.title,
        url=article.url,
        source=article.source,
        category=article.category,
        published=article.published,
        articles=[article],
    )


def _summary(story: Story) -> str:
    """给下游母版用的短摘要：一手报道（primary）的前几句。"""
    primary = story.primary
    text = " ".join(((primary.content if primary else "") or story.title).split())
    if not text:
        return ""
    sentences = [chunk for chunk in _SENTENCE_END.split(text) if chunk.strip()]
    joined = " ".join(sentence.strip() for sentence in sentences[:SUMMARY_SENTENCES]) if sentences else text
    if len(joined) > SUMMARY_CHARS:
        head = joined[:SUMMARY_CHARS]
        cut = head.rfind(" ")
        joined = (head[:cut] if cut > SUMMARY_CHARS // 2 else head).rstrip() + "…"
    return joined


def cluster(
    articles: Iterable[Any],
    settings: Settings | NormalizeSettings | Mapping[str, Any] | None = None,
) -> list[Story]:
    """按标题关键词的 Jaccard 做 single-link 聚类（不做任何过滤）。"""
    cfg = resolve_settings(settings)
    ordered = sorted(
        (item for item in (_coerce(article) for article in (articles or ())) if item is not None),
        key=lambda item: _timestamp(item.published),
        reverse=True,
    )

    stories: list[Story] = []
    member_tokens: list[list[set[str]]] = []
    for article in ordered:
        tokens = keywords(article.title or article.content)
        best_index, best_score = -1, 0.0
        for index, story in enumerate(stories):
            if len(story.articles) >= cfg.max_articles_per_story:
                continue
            score = max((similarity(tokens, other) for other in member_tokens[index]), default=0.0)
            if score > best_score:
                best_index, best_score = index, score
        if best_index >= 0 and tokens and best_score >= cfg.similarity:
            stories[best_index].add(article)
            member_tokens[best_index].append(tokens)
        else:
            stories.append(_new_story(article))
            member_tokens.append([tokens])

    for story in stories:
        story.summary = _summary(story)
    stories.sort(key=lambda story: (-story.article_count, -_timestamp(story.published)))
    log.info(
        "聚合完成：%s 篇 -> %s 个 Story（阈值 %.2f，最多 %s 篇/事件）",
        len(ordered),
        len(stories),
        cfg.similarity,
        cfg.max_articles_per_story,
    )
    return stories


def normalize(
    articles: Iterable[Any],
    settings: Settings | NormalizeSettings | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> list[Story]:
    """主入口：``List[Article]`` -> ``List[Story]``。

    Args:
        articles: fetcher 的产物（Article 或其 dict 形式）。
        settings: 阈值来源，可传 :class:`Settings` / :class:`NormalizeSettings` / 映射 / None。
        now: 时效基准时间，默认当前 UTC（测试里传固定值以保证确定性）。

    Returns:
        按“来源数多 → 时间新”排列的 Story；每个 ``Story.articles`` 含该事件下的各来源报道。
    """
    return cluster(prefilter(articles, settings, now=now), settings)


# ---------------------------------------------------------------- CLI
def load_articles(path: Path | str) -> list[Article]:
    """从 fetcher 写的 JSON（列表，或 ``{"articles": [...]}``）读回 Article。"""
    target = Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("articles") or []
    if not isinstance(payload, list):
        raise ValueError(f"{target} 里应该是 Article 列表")
    return [Article.from_dict(item) for item in payload if isinstance(item, Mapping)]


def _main(argv: Sequence[str] | None = None) -> int:
    """命令行跑一次聚合，方便排障：``python -m foundry.normalizer --hours 24``"""
    parser = argparse.ArgumentParser(description="把抓取的 Article 清洗、去重并聚合成 Story")
    parser.add_argument("--input", metavar="PATH", help="输入 JSON（默认 output/raw/articles.json）")
    parser.add_argument("--hours", type=int, default=None, help="只保留最近 N 小时（0 为不过滤）")
    parser.add_argument("--similarity", type=float, default=None, help="聚类相似度阈值（默认配置值）")
    parser.add_argument("--min-english", type=float, default=None, help="英文字符占比阈值（默认配置值）")
    parser.add_argument("--save", nargs="?", const="", metavar="PATH", help="结果写 JSON（默认 output/raw/stories.json）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    source = Path(args.input) if args.input else settings.output.raw / "articles.json"
    if not source.is_absolute():
        source = (settings.root / source).resolve()
    if not source.is_file():
        print(f"没有输入文件：{settings.rel(source)}（先跑 python -m foundry.fetcher --save）")
        return 1

    cfg = resolve_settings(settings)
    overrides = {
        "max_age_hours": cfg.max_age_hours if args.hours is None else args.hours,
        "similarity": cfg.similarity if args.similarity is None else args.similarity,
        "min_english_ratio": cfg.min_english_ratio if args.min_english is None else args.min_english,
        "max_articles_per_story": cfg.max_articles_per_story,
    }
    stories = normalize(load_articles(source), NormalizeSettings(**overrides))

    print(f"{source.name}: {len(stories)} 个 Story（最近 {overrides['max_age_hours']}h）")
    for index, story in enumerate(stories, start=1):
        sources = ", ".join(story.sources) or "-"
        print(f"  [{index}] {story.article_count} 篇 · {sources} · {story.title}")
        print(f"      {story.url}")

    if args.save is not None:
        target = Path(args.save) if args.save else settings.output.raw / "stories.json"
        if not target.is_absolute():
            target = (settings.root / target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = [story.to_dict() for story in stories]
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {settings.rel(target)}")
    return 0 if stories else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

"""核心数据模型：Article（一篇原文）、Story（同一事件的聚合）、ContentPack（母版产物）。

只依赖标准库。字段顺序与项目约定一致：

    Article      id, source, category, title, url, published, content, tags
    Story        id, title, summary, url, source, category, published, articles
    ContentPack  story_id, data, path
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from time import struct_time
from typing import Any, Iterable, Mapping

__all__ = [
    "Article",
    "ContentPack",
    "Story",
    "as_aware_datetime",
    "iso_or_none",
    "stable_id",
]


def stable_id(*parts: Any) -> str:
    """由若干字符串生成稳定短哈希（同一 URL 永远得到同一个 id）。"""
    joined = "|".join(str(part).strip() for part in parts if part not in (None, ""))
    return sha256(joined.encode("utf-8")).hexdigest()[:16]


def as_aware_datetime(value: Any) -> datetime | None:
    """把各种来源的时间统一成 timezone-aware 的 UTC datetime。

    接受 datetime、time.struct_time（feedparser）、Unix 时间戳、ISO8601、RFC 822 字符串。
    无时区的 datetime 按 UTC 处理；无法解析返回 None。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, struct_time):
        moment = datetime.fromtimestamp(calendar.timegm(value), timezone.utc)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                moment = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso_or_none(value: datetime | None) -> str | None:
    """datetime -> ISO8601 字符串（用于 JSON 序列化）。"""
    return value.isoformat() if isinstance(value, datetime) else None


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


@dataclass
class Article:
    """一篇抓到的英文原文。"""

    id: str = ""
    source: str = ""
    category: str = "news"
    title: str = ""
    url: str = ""
    published: datetime | None = None
    content: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.published = as_aware_datetime(self.published)
        self.tags = _text_list(self.tags)
        self.url = (self.url or "").strip()
        self.title = (self.title or "").strip()
        if not self.id:
            self.id = stable_id(self.url or self.title)

    @property
    def word_count(self) -> int:
        return len((self.content or "").split())

    def preview(self, length: int = 300) -> str:
        text = " ".join((self.content or "").split())
        return text if len(text) <= length else text[: length - 1].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "category": self.category,
            "title": self.title,
            "url": self.url,
            "published": iso_or_none(self.published),
            "content": self.content,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Article":
        return cls(
            id=str(data.get("id") or ""),
            source=str(data.get("source") or ""),
            category=str(data.get("category") or "news"),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            published=data.get("published"),
            content=str(data.get("content") or ""),
            tags=_text_list(data.get("tags")),
        )


@dataclass
class Story:
    """同一事件聚合并入多来源后的故事。"""

    id: str = ""
    title: str = ""
    summary: str = ""
    url: str = ""
    source: str = ""
    category: str = "news"
    published: datetime | None = None
    articles: list[Article] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.published = as_aware_datetime(self.published)
        self.url = (self.url or "").strip()
        self.title = (self.title or "").strip()
        if not self.id:
            self.id = stable_id(self.url or self.title)

    # ---- 聚合 ----
    def add(self, article: Article) -> None:
        """并入一篇原文，顺带补齐标题/链接/类别，并把 published 更新为最早时间。"""
        self.articles.append(article)
        self.title = self.title or article.title
        self.url = self.url or article.url
        self.source = self.source or article.source
        self.category = self.category or article.category
        if article.published and (self.published is None or article.published < self.published):
            self.published = article.published

    @property
    def primary(self) -> Article | None:
        """主文章：时间最早的一篇（没有时间的排最后）。"""
        if not self.articles:
            return None
        return min(self.articles, key=lambda item: item.published or datetime.max.replace(tzinfo=timezone.utc))

    @property
    def sources(self) -> list[str]:
        """去重后的来源名，保持出现顺序。"""
        seen: dict[str, None] = {}
        for article in self.articles:
            if article.source:
                seen.setdefault(article.source, None)
        return list(seen)

    @property
    def article_count(self) -> int:
        return len(self.articles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "source": self.source,
            "category": self.category,
            "published": iso_or_none(self.published),
            "articles": [article.to_dict() for article in self.articles],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Story":
        raw_articles = data.get("articles") or []
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            url=str(data.get("url") or ""),
            source=str(data.get("source") or ""),
            category=str(data.get("category") or "news"),
            published=data.get("published"),
            articles=[Article.from_dict(item) for item in raw_articles if isinstance(item, Mapping)],
        )


@dataclass
class ContentPack:
    """一个 Story 的 LLM 母版产物（levels/bilingual/vocabulary/... 全在这里）。"""

    story_id: str
    data: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def __post_init__(self) -> None:
        self.data = dict(self.data or {})
        self.path = Path(self.path) if self.path is not None else None

    # ---- 读取 ----
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        """键存在且内容非空。"""
        if key not in self.data:
            return False
        value = self.data[key]
        return value not in (None, "", [], {}, 0)

    def keys(self) -> list[str]:
        return list(self.data)

    # ---- 读写文件 ----
    def write(self, path: Path | str | None = None) -> Path:
        """把母版 JSON 写到 path（默认用已有 path）；返回实际写入路径。"""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("ContentPack.write 需要 path（或构造时传入 path）")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"story_id": self.story_id, **self.data} if "story_id" not in self.data else self.data
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path = target
        return target

    @classmethod
    def load(cls, path: Path | str) -> "ContentPack":
        """从 JSON 文件读取母版；story_id 取文件名的 <story_id>.json。"""
        target = Path(path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        data = dict(payload) if isinstance(payload, Mapping) else {}
        story_id = str(data.pop("story_id", "") or target.stem)
        return cls(story_id=story_id, data=data, path=target)

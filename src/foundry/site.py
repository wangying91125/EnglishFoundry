"""静态站点生成：``output/`` 产物 → ``web/`` 纯静态站点（Jinja2 渲染）。

分工与约定：
    * 输入只有 ``output/`` 里的现成产物：``packs/*.json``（母版）、``posts/**/*.md``（角色 Markdown 与
      平台文案）、``audio/*.mp3`` + ``audio/manifest.json``（配音）、``last_run.json``（运行摘要，可缺），
      角色表来自 ``config/roles.yaml``、平台表来自 ``config/platforms.yaml``。本模块**不联网、不调 LLM、
      不产新内容**，只做“读产物 → 排版 → 写 HTML”。
    * 输出是纯静态站点：``web/index.html``（今日精选 + 角色/类别筛选 + 客户端搜索）、
      ``web/story/<story_id>.html``（分级阅读 + 双语精读 + 词汇 + 长难句 + 背景 + 听力 + 口语 + 写作 + 练习）、
      ``web/role/<role_key>.html``（角色专稿）、``web/about.html``，外加 ``web/static/``（原生 CSS/JS）和
      ``web/audio/``（mp3 副本，这样 ``web/`` 整目录能单独部署）。
    * 模板放在 ``web/templates/``（随仓库走、可直接改），找不到时退回包内同路径；两处的 ``base.html``
      都不存在才抛 :class:`SiteError` 给可操作提示，绝不静默产出空站点。
    * 交互全靠 ``static/app.js``（原生 JS，无框架无 CDN）：搜索与筛选在客户端完成，数据就挂在卡片的
      ``data-*`` 属性上，**不 fetch 任何 JSON**，所以双击 ``file://`` 打开也好用。
    * 母版 JSON 只有 9 个内容键，标题/来源/类别/时间这些元信息不保证有：能读就读（兼容顶层键和
      ``meta`` / ``story`` 段），读不到就用 B1 标题、``news`` 类别、文件修改时间兜底；缺哪个板块就少
      哪一段，不会整体崩掉。
    * 角色 Markdown 优先用 ``output/posts`` 里现成的（离线、可复现），没有才现场调
      :func:`foundry.roles.render_for_role` 渲染，保证角色页永远有内容。

用法::

    from foundry.config import load_settings
    from foundry.site import build_site

    written = build_site(load_settings())            # 读 output/ 的产物
    written = build_site(settings, packs=[pack])     # 直接喂母版（测试 / dry-run 用）

手工跑一次：``python -m foundry.site``（渲染并把生成的文件清单打印出来）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as _escape_html
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from foundry.config import Settings, load_settings
from foundry.master import CEFR_LEVELS
from foundry.models import ContentPack, as_aware_datetime
from foundry.platforms import Platform, PlatformsError, load_platforms
from foundry.roles import Role, RolesError, load_roles, render_for_role, topic_match
from foundry.tts import safe_name

try:  # count_words 是角色模块的中英混排计数，站点用它算正文词数与阅读时长
    from foundry.roles import count_words
except ImportError:  # pragma: no cover - roles 只依赖标准库 + yaml，正常不会缺
    count_words = lambda text: len((text or "").split())  # noqa: E731

__all__ = [
    "AUDIO_DIR_NAME",
    "DEFAULT_LEVEL",
    "NAV_ITEMS",
    "SITE_NAME",
    "STATIC_DIR_NAME",
    "TEMPLATE_DIR_NAME",
    "AudioClip",
    "SiteError",
    "SiteStory",
    "build_site",
    "collect_stories",
    "discover_posts",
    "load_audio_clips",
    "load_packs",
    "markdown_to_html",
    "story_from_pack",
    "template_dir",
]

log = logging.getLogger("foundry.site")

SITE_NAME = "EnglishFoundry"
SITE_TAGLINE = "Daily English, levelled for you"
SITE_TAGLINE_ZH = "每日全球英文 · 分级精读 · 听力跟读"
DEFAULT_LEVEL = "B1"
FALLBACK_CATEGORY = "news"
MIN_LEVEL = "A1"
MAX_QUIZ_LETTERS = "ABCDEFGH"
SEARCH_HAYSTACK_CHARS = 1400
MAX_ROLE_TAGS = 6
READING_WORDS_PER_MINUTE = 160
MANIFEST_NAME = "manifest.json"
AUDIO_DIR_NAME = "audio"
STATIC_DIR_NAME = "static"
TEMPLATE_DIR_NAME = "templates"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: 顶部导航：英文为主、中文作小字说明（Requirement: 英文内容为主 + 中文导航说明）
NAV_ITEMS: tuple[Mapping[str, str], ...] = (
    {"href": "index.html", "en": "Today", "zh": "今日精选"},
    {"href": "index.html#library", "en": "Stories", "zh": "分级阅读"},
    {"href": "index.html#listening", "en": "Listening", "zh": "听力下载"},
    {"href": "index.html#roles", "en": "Roles", "zh": "角色定制"},
    {"href": "about.html", "en": "About", "zh": "关于本站"},
)

#: 板块英文名 → 中文名（about / 各处小标题共用）
MODULE_LABELS: Mapping[str, str] = {
    "levels": "分级阅读",
    "bilingual": "双语精读",
    "vocabulary": "核心词汇",
    "grammar": "长难句",
    "background_zh": "背景速览",
    "listening_script": "听力稿",
    "speaking": "开口说",
    "writing": "写作",
    "quiz": "小练习",
}


class SiteError(RuntimeError):
    """站点生成失败（模板缺失、目标目录不可写等可操作问题）。"""


# ---------------------------------------------------------------- 小工具
def _as_text(value: Any, default: str = "") -> str:
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return default
    text = str(value).strip()
    return text or default


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _records(value: Any) -> list[dict[str, Any]]:
    """取列表里的映射项（对象/标量丢掉，保证模板拿到的永远是 dict）。"""
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in value.splitlines() if item.strip()]
    if not isinstance(value, (list, tuple)):
        return [] if value in (None, "") else [str(value)]
    return [_as_text(item) for item in value if _as_text(item)]


def _page_name(identifier: Any) -> str:
    return safe_name(identifier) or "page"


def _humanize(identifier: Any) -> str:
    """``eu-data-fine`` -> ``Eu Data Fine``（音频没有清单标签时用）。"""
    words = [word for word in re.split(r"[^A-Za-z0-9]+", _as_text(identifier)) if word]
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _first_sentences(text: str, count: int = 2, char_limit: int = 260) -> str:
    """取前几句当摘要（母版没有 summary 字段时的兜底）。"""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", flat)
    joined = " ".join(parts[: max(1, count)])
    return _clip(joined, char_limit)


def _minutes(words: int) -> str:
    if words <= 0:
        return ""
    return f"{max(1, round(words / READING_WORDS_PER_MINUTE))} min"


# ---------------------------------------------------------------- Markdown → HTML
#: 角色 Markdown 里有 ``<details>`` 折叠答案，先整体转义、再按白名单放开这两个标签
_ALLOWED_TAGS = ("details", "summary")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,}|={3,})$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_DEL_RE = re.compile(r"~~(.+?)~~")
_TAG_RE = re.compile(r"&lt;(/?)(" + "|".join(_ALLOWED_TAGS) + r")\b([^&]*?)&gt;")
_UNSAFE_SCHEME_RE = re.compile(r"^(?:javascript|data|vbscript)\s*:", re.I)


def _allow_tags(escaped: str) -> str:
    """把转义过的 ``<details>`` / ``<summary>`` 还原成真标签（其余标签保持转义）。"""
    return _TAG_RE.sub(lambda match: f"<{match.group(1)}{match.group(2)}{match.group(3)}>", escaped)


def _safe_href(href: str) -> str:
    text = (href or "").strip()
    return "#" if _UNSAFE_SCHEME_RE.match(text) else _escape_html(text, quote=True)


def _inline(text: str) -> str:
    """行内语法：转义在先，代码 / 链接 / 加粗 / 斜体 / 删除线在后（所以永远不会有注入）。"""
    out = _allow_tags(_escape_html(_as_text(text), quote=False))
    out = _CODE_RE.sub(lambda match: f"<code>{match.group(1)}</code>", out)
    out = _LINK_RE.sub(
        lambda match: f'<a href="{_safe_href(match.group(2))}" target="_blank" rel="noopener noreferrer">{match.group(1)}</a>',
        out,
    )
    out = _BOLD_RE.sub(lambda match: f"<strong>{match.group(1)}</strong>", out)
    out = _EM_RE.sub(lambda match: f"<em>{match.group(1)}</em>", out)
    out = _DEL_RE.sub(lambda match: f"<del>{match.group(1)}</del>", out)
    return out


def _split_row(line: str) -> list[str]:
    row = line.strip().strip("|")
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", row)]


def _render_table(lines: Sequence[str], start: int) -> tuple[str, int]:
    """管道表格 -> ``<table>``；第二行的 ``|---|`` 是分界线，表头取第一行。"""
    rows: list[list[str]] = []
    index = start
    while index < len(lines) and _TABLE_ROW_RE.match(lines[index]):
        if not _TABLE_SEP_RE.match(lines[index]):
            rows.append(_split_row(lines[index]))
        index += 1
    if len(rows) < 2:
        return "", index
    width = max(len(row) for row in rows)
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row + [""] * (width - len(row))) + "</tr>"
        for row in rows[1:]
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>', index


def _render_list(items: Sequence[tuple[int, bool, str]]) -> str:
    """列表渲染（支持缩进一层子列表）：``items`` 是 ``(缩进, 是否有序, 文本)``。"""
    if not items:
        return ""
    entries: list[list[Any]] = []
    for indent, ordered, text in items:
        if indent <= 1 or not entries:
            entries.append([ordered, text, []])
        else:
            entries[-1][2].append((max(indent - 2, 0), ordered, text))
    out: list[str] = []
    cursor = 0
    while cursor < len(entries):
        ordered = entries[cursor][0]
        tag = "ol" if ordered else "ul"
        parts = [f"<{tag}>"]
        while cursor < len(entries) and entries[cursor][0] == ordered:
            _, text, children = entries[cursor]
            inner = _inline(text)
            if children:
                inner += _render_list(children)
            parts.append(f"<li>{inner}</li>")
            cursor += 1
        parts.append(f"</{tag}>")
        out.append("".join(parts))
    return "".join(out)


def _starts_block(line: str) -> bool:
    text = line.strip()
    return bool(
        _HEADING_RE.match(text)
        or _HR_RE.match(text)
        or _UL_RE.match(line)
        or _OL_RE.match(line)
        or text.startswith((">", "|", "```", "<"))
    )


def markdown_to_html(markdown: Any) -> Markup:
    """角色 / 平台 Markdown 的极简离线渲染（标题、列表、表格、引用、折叠块、行内强调）。

    先整体 HTML 转义再按白名单放开 ``<details>`` / ``<summary>``，链接只允许安全协议，
    所以产物里有怪东西也不会变成注入。返回 :class:`markupsafe.Markup`，模板里可直接用。
    """
    lines = _as_text(markdown).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        text = line.strip()
        if not text:
            index += 1
            continue
        if text.startswith("```"):
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append(f"<pre><code>{_escape_html(chr(10).join(body))}</code></pre>")
            continue
        heading = _HEADING_RE.match(text)
        if heading:
            depth = min(max(len(heading.group(1)) + 1, 2), 6)  # 页面已有 h1，正文从 h2 起
            blocks.append(f"<h{depth}>{_inline(heading.group(2).strip())}</h{depth}>")
            index += 1
            continue
        if _HR_RE.match(text):
            blocks.append("<hr>")
            index += 1
            continue
        if _TABLE_ROW_RE.match(line) and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1]):
            table, index = _render_table(lines, index)
            if table:
                blocks.append(table)
            continue
        if text.startswith(">"):
            quotes: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quotes.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append("<blockquote>" + "".join(f"<p>{_inline(item)}</p>" for item in quotes if item) + "</blockquote>")
            continue
        if _UL_RE.match(line) or _OL_RE.match(line):
            items: list[tuple[int, bool, str]] = []
            while index < len(lines):
                ul, ol = _UL_RE.match(lines[index]), _OL_RE.match(lines[index])
                if not (ul or ol):
                    break
                match = ul or ol
                items.append((len(match.group(1)), bool(ol), match.group(2).strip()))
                index += 1
            blocks.append(_render_list(items))
            continue
        if text.startswith("<"):
            blocks.append(_allow_tags(_escape_html(text, quote=False)))
            index += 1
            continue
        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip() and not _starts_block(lines[index]):
            paragraph.append(lines[index].strip())
            index += 1
        blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
    return Markup("\n".join(blocks))


# ---------------------------------------------------------------- 产物 → 站点数据
#: 常见类别的中文说明（站点 chip 上英文为主、中文做小字）
CATEGORY_LABELS: Mapping[str, str] = {
    "news": "新闻",
    "world": "国际",
    "tech": "科技",
    "business": "商业",
    "finance": "财经",
    "markets": "市场",
    "politics": "政治",
    "policy": "政策",
    "law": "法律",
    "science": "科学",
    "health": "健康",
    "climate": "气候",
    "environment": "环境",
    "energy": "能源",
    "sports": "体育",
    "culture": "文化",
    "analysis": "深度",
    "opinion": "观点",
}


@dataclass(frozen=True)
class AudioClip:
    """一段配音（来自 ``output/audio/manifest.json``，或目录里裸扫到的 mp3）。"""

    key: str
    filename: str
    label: str = ""
    voice: str = ""
    rate: str = ""
    seconds: float = 0.0
    size: int = 0
    path: Path | None = None

    @property
    def url(self) -> str:
        """站点内的相对路径（``web/audio/<file>``）。"""
        return f"{AUDIO_DIR_NAME}/{self.filename}"

    @property
    def search(self) -> str:
        return f"{self.key} {self.filename}".lower()

    @property
    def duration_label(self) -> str:
        if self.seconds <= 0:
            return ""
        total = int(round(self.seconds))
        return f"{total // 60}:{total % 60:02d}"

    @property
    def size_label(self) -> str:
        if self.size <= 0:
            return ""
        if self.size >= 1_048_576:
            return f"{self.size / 1_048_576:.1f} MB"
        return f"{max(1, int(round(self.size / 1024)))} KB"


@dataclass
class SiteStory:
    """一个母版内容包在站点上的样子（详情页 + 首页卡片 + 角色页共用）。"""

    id: str
    title: str
    summary: str = ""
    category: str = FALLBACK_CATEGORY
    source: str = ""
    url: str = ""
    published: datetime | None = None
    tags: tuple[str, ...] = ()
    levels: list[dict[str, Any]] = field(default_factory=list)
    default_level: str = DEFAULT_LEVEL
    bilingual: list[dict[str, str]] = field(default_factory=list)
    vocabulary: list[dict[str, str]] = field(default_factory=list)
    grammar: list[dict[str, str]] = field(default_factory=list)
    background_zh: str = ""
    listening_script: str = ""
    speaking: dict[str, Any] = field(default_factory=dict)
    writing: dict[str, Any] = field(default_factory=dict)
    quiz: list[dict[str, Any]] = field(default_factory=list)
    audio: list[AudioClip] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    role_names: dict[str, str] = field(default_factory=dict)
    role_scores: dict[str, float] = field(default_factory=dict)
    role_bodies: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None
    pack: ContentPack | None = None

    # ---- 模板用派生值 ----
    @property
    def href(self) -> str:
        return f"story/{_page_name(self.id)}.html"

    @property
    def level_labels(self) -> list[str]:
        return [str(item["level"]) for item in self.levels]

    @property
    def category_zh(self) -> str:
        return CATEGORY_LABELS.get(self.category.lower(), "")

    @property
    def category_en(self) -> str:
        return _humanize(self.category) or "News"

    @property
    def date_label(self) -> str:
        return self.published.strftime("%Y-%m-%d") if self.published else ""

    @property
    def iso_date(self) -> str:
        return self.published.date().isoformat() if self.published else ""

    @property
    def words_total(self) -> int:
        return int(sum(int(item.get("words") or 0) for item in self.levels))

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def search(self) -> str:
        """卡片 ``data-search`` 用的检索文本（客户端搜索的全部数据来源）。"""
        return _search_haystack(self)

    @property
    def role_pages(self) -> list[dict[str, str]]:
        """本篇适配的角色及其角色页链接。"""
        return [{"key": key, "name": self.role_names.get(key, _humanize(key)), "href": f"role/{_page_name(key)}.html"} for key in self.roles]

    @property
    def audio_label(self) -> str:
        return self.audio[0].label if self.audio else ""

    @property
    def sections(self) -> list[str]:
        """这篇真正有内容的板块（模板据此决定显示哪些小节）。"""
        found = []
        if self.levels:
            found.append("levels")
        for name, value in (
            ("background_zh", self.background_zh),
            ("bilingual", self.bilingual),
            ("vocabulary", self.vocabulary),
            ("grammar", self.grammar),
            ("listening_script", self.listening_script),
            ("speaking", self.speaking),
            ("writing", self.writing),
            ("quiz", self.quiz),
        ):
            if value:
                found.append(name)
        return found

    def body(self, level: str | None = None) -> dict[str, Any]:
        """取某档内容（没有该档就退回默认档 / 第一档）。"""
        wanted = (level or self.default_level).upper()
        for item in self.levels:
            if item["level"] == wanted:
                return item
        for item in self.levels:
            if item["level"] == DEFAULT_LEVEL:
                return item
        return self.levels[0] if self.levels else {"level": DEFAULT_LEVEL, "title": self.title, "body": "", "words": 0}


def _meta_value(data: Mapping[str, Any], *names: str) -> str:
    """从母版顶层或 ``meta`` / ``story`` 段里取一个字符串字段。"""
    nests = [data] + [value for value in (data.get("meta"), data.get("story")) if isinstance(value, Mapping)]
    for nest in nests:
        for name in names:
            value = nest.get(name)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
    return ""


#: CEFR 档位的中文说明（详情页 tab 的小字）
LEVEL_HINTS: Mapping[str, str] = {
    "A1": "入门",
    "A2": "基础",
    "B1": "进阶",
    "B2": "中高级",
    "C1": "高级",
    "C2": "精通",
}


def _resolve(settings: Settings | None, value: Any, default: str = "") -> Path:
    """把配置里的路径变成绝对路径（相对路径按项目根解析）。"""
    if value is None:
        value = default
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and settings is not None:
        path = settings.root / path
    return path.resolve()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warning("读不了 %s：%s", path.name, error)
        return None


def _coerce_pack(item: Any) -> ContentPack | None:
    """把 packs 参数里的各种形状变成 :class:`ContentPack`（对象 / 映射 / JSON 路径都收）。"""
    if isinstance(item, ContentPack):
        return item
    if isinstance(item, (str, Path)):
        target = Path(item).expanduser()
        if not target.is_file():
            log.warning("母版文件不存在，跳过：%s", target)
            return None
        try:
            return ContentPack.load(target)
        except (OSError, ValueError) as error:
            log.warning("母版 JSON 解析失败，跳过 %s：%s", target.name, error)
            return None
    if isinstance(item, Mapping):
        data = dict(item)
        story_id = _as_text(data.get("story_id") or data.get("id")) or "pack"
        data.pop("story_id", None)
        return ContentPack(story_id=story_id, data=data)
    log.warning("无法识别的母版类型：%s", type(item).__name__)
    return None


def load_packs(directory: str | Path) -> list[ContentPack]:
    """读 ``output/packs/*.json``；坏文件只跳过并告警，不整体失败。"""
    root = Path(directory).expanduser()
    if not root.is_dir():
        log.warning("母版目录不存在：%s", root)
        return []
    packs: list[ContentPack] = []
    for path in sorted(root.glob("*.json")):
        pack = _coerce_pack(path)
        if pack is not None:
            packs.append(pack)
    return packs


def _normalize_levels(value: Any, fallback_title: str) -> list[dict[str, Any]]:
    """``levels`` -> 按 CEFR 顺序的档位列表（未知档位追加在后面）。"""
    raw = value if isinstance(value, Mapping) else {}
    ordered = [level for level in CEFR_LEVELS if level in raw]
    ordered += [level for level in raw if _as_text(level).upper() not in CEFR_LEVELS and level not in CEFR_LEVELS]
    levels: list[dict[str, Any]] = []
    for level in ordered:
        entry = raw.get(level)
        entry = dict(entry) if isinstance(entry, Mapping) else {}
        title = _as_text(entry.get("title")) or fallback_title
        body = _as_text(entry.get("body"))
        if not title and not body:
            continue
        code = _as_text(level).upper()
        words = count_words(body)
        levels.append(
            {"level": code, "title": title, "body": body, "words": words, "minutes": _minutes(words), "hint": LEVEL_HINTS.get(code, "")}
        )
    return levels


def _choice_pairs(value: Any) -> list[dict[str, str]]:
    """练习题选项：兼容 ``["文本", ...]`` / ``{"A": "文本"}`` / ``[{"letter","text"}]`` / ``"A. 文本"``。"""
    pairs: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        entries = list(value.items())
    elif isinstance(value, (list, tuple)):
        entries = [(None, item) for item in value]  # type: ignore[misc]
    else:
        entries = []
    for position, (key, raw) in enumerate(entries):
        letter = _as_text(key).upper()[:1]
        text = ""
        if isinstance(raw, Mapping):
            letter = _as_text(raw.get("letter") or raw.get("key") or raw.get("label")).upper()[:1] or letter
            text = _as_text(raw.get("text") or raw.get("value") or raw.get("choice") or raw.get("option"))
        elif isinstance(raw, str):
            text = raw.strip()
            match = re.match(r"^\s*([A-Ha-h])[).:]\s*(.*)$", text)
            if match:
                letter = match.group(1).upper() or letter
                text = match.group(2).strip()
        else:
            text = _as_text(raw)
        if not text:
            continue
        if not letter or letter not in MAX_QUIZ_LETTERS:
            letter = MAX_QUIZ_LETTERS[min(position, len(MAX_QUIZ_LETTERS) - 1)]
        pairs.append({"letter": letter, "text": text})
        if len(pairs) >= len(MAX_QUIZ_LETTERS):
            break
    return pairs


def _answer_letter(value: Any, pairs: Sequence[Mapping[str, str]]) -> str:
    """答案位置归一成字母：兼容 ``"B"`` / ``"答案：B"`` / ``1`` / ``"2."``。"""
    letters = [str(item.get("letter")) for item in pairs] or list(MAX_QUIZ_LETTERS)
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        index = value if value == 0 else value - 1  # 0 基与 1 基都认（模型两种写法都会出）
        return letters[index] if 0 <= index < len(letters) else ""
    text = _as_text(value).upper()
    match = re.search(r"\b([A-H])\b", text)
    if match and match.group(1) in letters:
        return match.group(1)
    if text[:1].isdigit():
        index = int(text[0]) - 1
        if 0 <= index < len(letters):
            return letters[index]
    return text[:1] if text[:1] in letters else ""


def _normalize_quiz(value: Any) -> list[dict[str, Any]]:
    quiz: list[dict[str, Any]] = []
    for item in _records(value):
        question = _as_text(item.get("q") or item.get("question") or item.get("stem"))
        if not question:
            continue
        pairs = _choice_pairs(item.get("choices") or item.get("options"))
        quiz.append(
            {
                "q": question,
                "choices": pairs,
                "answer": _answer_letter(item.get("answer") or item.get("key"), pairs),
                "explain": _as_text(item.get("explain") or item.get("explanation") or item.get("note")),
            }
        )
    return quiz


def _normalize_speaking(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        questions = _strings(value)
        return {"questions": questions, "roleplay": {}} if questions else {}
    roleplay = value.get("roleplay")
    roleplay = dict(roleplay) if isinstance(roleplay, Mapping) else {}
    if roleplay and not any(_as_text(item) for item in roleplay.values()):
        roleplay = {}
    out = {"questions": _strings(value.get("questions") or value.get("questions_en"))}
    if roleplay:
        out["roleplay"] = {
            "title": _as_text(roleplay.get("title")),
            "scenario": _as_text(roleplay.get("scenario")),
            "roles": _strings(roleplay.get("roles")),
            "task": _as_text(roleplay.get("task")),
        }
    return out if out["questions"] or out.get("roleplay") else {}


def _normalize_writing(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"essay": value} if len(value.split()) > 12 else {"topic": value}
    if not isinstance(value, Mapping):
        return {}
    topic = _as_text(value.get("topic") or value.get("prompt") or value.get("title"))
    essay = _as_text(value.get("essay") or value.get("sample") or value.get("model"))
    words = count_words(essay)
    return {"topic": topic, "essay": essay, "words": words} if (topic or essay) else {}


def story_from_pack(pack: Any, *, roles: Mapping[str, Role] | None = None, settings: Settings | None = None) -> SiteStory:
    """一个母版内容包 → :class:`SiteStory`（板块缺什么就少什么，元信息缺什么就兜底）。"""
    coerced = _coerce_pack(pack)
    if coerced is None:
        raise SiteError("无法把输入转成 ContentPack")
    data = coerced.data
    levels = _normalize_levels(data.get("levels"), "")
    default_level = next((item["level"] for item in levels if item["level"] == DEFAULT_LEVEL), "")
    if not default_level:
        middle = len(levels) // 2
        default_level = levels[middle]["level"] if levels else DEFAULT_LEVEL
    body_default = next((item for item in levels if item["level"] == default_level), levels[0] if levels else {})
    title = _meta_value(data, "title", "headline") or _as_text(body_default.get("title")) or coerced.story_id.replace("-", " ")
    body_text = _as_text(body_default.get("body"))
    summary = _meta_value(data, "summary", "lead", "dek") or _first_sentences(body_text)
    published_raw = _meta_value(data, "published", "date", "published_at")
    published = as_aware_datetime(published_raw) if published_raw else None
    if published is None and coerced.path is not None and Path(coerced.path).is_file():
        published = datetime.fromtimestamp(Path(coerced.path).stat().st_mtime, timezone.utc)
    if published is None:
        published = datetime.now(timezone.utc)
    category = (_meta_value(data, "category", "topic") or FALLBACK_CATEGORY).lower()

    vocabulary = [
        {
            "word": _as_text(item.get("word") or item.get("vocabulary")),
            "pos": _as_text(item.get("pos") or item.get("part_of_speech")),
            "zh": _as_text(item.get("zh") or item.get("zh_def")),
            "en_def": _as_text(item.get("en_def") or item.get("definition")),
            "example": _as_text(item.get("example")),
        }
        for item in _records(data.get("vocabulary"))
    ]
    vocabulary = [item for item in vocabulary if item["word"]]
    bilingual = [
        {"en": _as_text(item.get("en")), "zh": _as_text(item.get("zh"))}
        for item in _records(data.get("bilingual"))
    ]
    bilingual = [item for item in bilingual if item["en"] or item["zh"]]
    grammar = [
        {
            "sentence": _as_text(item.get("sentence")),
            "point": _as_text(item.get("point")),
            "explanation_zh": _as_text(item.get("explanation_zh") or item.get("explanation")),
        }
        for item in _records(data.get("grammar"))
    ]
    grammar = [item for item in grammar if item["sentence"] or item["explanation_zh"]]

    story = SiteStory(
        id=coerced.story_id or "pack",
        title=title,
        summary=summary,
        category=category,
        source=_meta_value(data, "source", "outlet", "site"),
        url=_meta_value(data, "url", "link", "canonical_url"),
        published=published,
        tags=tuple(_strings(data.get("tags"))),
        levels=levels,
        default_level=default_level,
        bilingual=bilingual,
        vocabulary=vocabulary,
        grammar=grammar,
        background_zh=_as_text(data.get("background_zh")),
        listening_script=_as_text(data.get("listening_script")),
        speaking=_normalize_speaking(data.get("speaking")),
        writing=_normalize_writing(data.get("writing")),
        quiz=_normalize_quiz(data.get("quiz")),
        path=Path(coerced.path) if coerced.path is not None else None,
        pack=coerced,
    )
    story.roles = _fit_roles(story, roles or {})
    return story


def _fit_roles(story: SiteStory, roles: Mapping[str, Role]) -> list[str]:
    """按题材契合度给这篇故事打上角色标签（一个都没命中时退回到难度偏好匹配）。"""
    if not roles:
        return []
    story.role_names = {key: (role.name or _humanize(key)) for key, role in roles.items()}
    scores: dict[str, float] = {}
    for key, role in roles.items():
        try:
            scores[key] = float(topic_match(story.pack, role, roles)) if story.pack is not None else 0.0
        except Exception as error:  # RolesError / 数据怪：只影响标签，不影响出站
            log.debug("topic_match(%s, %s) 失败：%s", story.id, key, error)
            scores[key] = 0.0
    story.role_scores = scores
    matched = [key for key, value in scores.items() if value > 0]
    matched.sort(key=lambda key: (-scores[key], list(roles).index(key)))
    if matched:
        return matched[:MAX_ROLE_TAGS]
    available = set(story.level_labels)
    fallback = [key for key, role in roles.items() if available & set(role.levels)]
    return fallback[:MAX_ROLE_TAGS]


def _clip_label(key: Any, names: Mapping[str, str] | None = None) -> str:
    """音频标签：把 key 里的角色 / 平台名换成展示名（``business`` → ``商务职场``）。"""
    table = dict(names or {})
    haystack = f" {_as_text(key).lower()} "
    matched: list[str] = []
    for token in sorted(table, key=len, reverse=True):
        needle = token.lower()
        if needle and re.search(rf"[^a-z0-9]{re.escape(needle)}[^a-z0-9]", haystack):
            matched.append(table[token])
            haystack = " ".join(part for part in haystack.split() if needle != part)
    if matched:
        return " · ".join(dict.fromkeys(matched))
    return _humanize(key)


def load_audio_clips(
    settings: Settings | None = None,
    *,
    directory: str | Path | None = None,
    names: Mapping[str, str] | None = None,
) -> list[AudioClip]:
    """音频清单：优先 ``manifest.json``（带时长/字节/口音），再补扫目录里没登记的非空 mp3。"""
    loaded = settings if settings is not None else load_settings()
    root = _resolve(loaded, directory, str(loaded.output.audio))
    if not root.is_dir():
        log.info("还没有配音目录：%s", loaded.rel(root))
        return []
    clips: list[AudioClip] = []
    taken: set[str] = set()
    manifest = root / MANIFEST_NAME
    payload = _read_json(manifest) if manifest.is_file() else None
    rows = payload.get("items") if isinstance(payload, Mapping) else None
    for row in _records(rows):
        if row.get("ok") is False:
            log.debug("跳过失败的音频：%s", row.get("key"))
            continue
        key = _as_text(row.get("key") or row.get("name"))
        filename = _as_text(row.get("file")) or f"{safe_name(key) or 'clip'}.mp3"
        path = root / Path(filename).name
        if not path.is_file() or path.stat().st_size <= 0:
            log.debug("清单里的音频不在磁盘上，跳过：%s", filename)
            continue
        taken.add(path.name)
        clips.append(
            AudioClip(
                key=key or path.stem,
                filename=path.name,
                label=_clip_label(key or path.stem, names),
                voice=_as_text(row.get("voice")),
                rate=_as_text(row.get("rate")),
                seconds=float(row.get("seconds") or 0) if isinstance(row.get("seconds"), (int, float)) else 0.0,
                size=int(row.get("bytes") or 0) if isinstance(row.get("bytes"), (int, float)) else path.stat().st_size,
                path=path,
            )
        )
    for path in sorted(root.glob("*.mp3")):
        if path.name in taken or path.stat().st_size <= 0:
            continue
        clips.append(AudioClip(key=path.stem, filename=path.name, label=_clip_label(path.stem, names), size=path.stat().st_size, path=path))
    log.info("音频：%s 段可用（%s）", len(clips), loaded.rel(root))
    return clips


def discover_posts(
    directory: str | Path | None = None,
    *,
    settings: Settings | None = None,
    role_keys: Iterable[str] = (),
    platform_keys: Iterable[str] = (),
    story_ids: Iterable[str] = (),
) -> dict[tuple[str, str], dict[str, Any]]:
    """在 ``output/posts`` 里认领现成的角色 Markdown 与平台文案。

    认法（只看目录名与文件名，不解析内容）：

    * 文件名是角色 key（``posts/business.md``、``posts/<story_id>/business.md``）→ 该角色的 Markdown；
    * 文件名是平台 key（``posts/business/xiaohongshu.md``）→ 平台文案，角色取父目录名；
    * 路径里出现的目录名如果等于某个 ``story_id``，文案就归属那个故事，否则算“通用”（只有一份母版时够用）。

    返回 ``{(story_id 或 "*", role_key 或 "*"): {"markdown": str, "platforms": {平台 key: 文案}}}``。
    """
    loaded = settings if settings is not None else load_settings()
    root = _resolve(loaded, directory, str(loaded.output.posts))
    known_roles = {str(key) for key in role_keys}
    known_platforms = {str(key) for key in platform_keys}
    known_stories = {str(key) for key in story_ids}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.md")):
        parts = list(path.relative_to(root).parts)
        stem = Path(parts[-1]).stem
        dirs = parts[:-1]
        story_id = next((item for item in dirs if item in known_stories), "*")
        role_key = stem if stem in known_roles else next((item for item in reversed(dirs) if item in known_roles), "*")
        platform_key = stem if stem in known_platforms else ""
        if platform_key == "" and role_key == "*" and stem not in known_stories:
            log.debug("认不出归属的 Markdown，跳过：%s", "/".join(parts))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            log.warning("读不了 %s：%s", path, error)
            continue
        if platform_key:
            entry = found.setdefault((story_id, role_key), {"markdown": "", "markdown_from": "", "platforms": {}})
            entry["platforms"][platform_key] = text
        else:
            entry = found.setdefault((story_id, role_key), {"markdown": "", "markdown_from": "", "platforms": {}})
            entry["markdown"] = text
            entry["markdown_from"] = loaded.rel(path)
    log.info("平台/角色文案：认领到 %s 组（%s）", len(found), loaded.rel(root))
    return found


def assign_audio(stories: Sequence[SiteStory], clips: Sequence[AudioClip]) -> list[AudioClip]:
    """把音频按 story_id 挂到故事上；只有一篇时全归它，剩下的留给首页听力区列出。"""
    claimed: set[str] = set()
    for story in stories:
        needle = safe_name(story.id).lower()
        if not needle:
            continue
        for clip in clips:
            if clip.filename in claimed:
                continue
            if needle in clip.search:
                story.audio.append(clip)
                claimed.add(clip.filename)
    leftovers = [clip for clip in clips if clip.filename not in claimed]
    if leftovers and len(stories) == 1:
        stories[0].audio.extend(leftovers)
        return []
    if leftovers:
        log.info("%s 段音频没匹配到具体故事，只在首页 Listening 区列出", len(leftovers))
    return leftovers


def _search_haystack(story: SiteStory) -> str:
    """卡片 ``data-search``：标题 + 摘要 + 词表 + 正文前段，小写、压平空白、限长。"""
    parts = [story.id, story.title, story.summary, story.category, story.source]
    parts += [item["word"] for item in story.vocabulary]
    parts += [item["title"] for item in story.levels]
    parts += [item["hint"] for item in story.levels]
    parts += [story.background_zh]
    for item in story.levels:
        parts.append(_clip(item["body"], 400))
    return _clip(" ".join(part for part in parts if part), SEARCH_HAYSTACK_CHARS).lower()


def _config_tables(settings: Settings) -> tuple[dict[str, Role], dict[str, Platform]]:
    """角色表 / 平台表：配置缺失时降级为空表并告警（站点仍要能出）。"""
    try:
        roles = load_roles(settings=settings)
    except (RolesError, FileNotFoundError) as error:
        log.warning("读不到角色配置，角色页将省略：%s", error)
        roles = {}
    try:
        platforms = load_platforms(settings=settings)
    except PlatformsError as error:
        log.warning("读不到平台配置：%s", error)
        platforms = {}
    return roles, platforms


def _display_names(roles: Mapping[str, Role], platforms: Mapping[str, Platform]) -> dict[str, str]:
    names: dict[str, str] = {}
    for key, role in roles.items():
        names[key] = " ".join(part for part in (role.emoji, role.name or key) if part).strip()
    for key, platform in platforms.items():
        names.setdefault(key, " ".join(part for part in (platform.emoji, platform.name or key) if part).strip())
    return names


def attach_role_bodies(
    stories: Sequence[SiteStory],
    *,
    roles: Mapping[str, Role],
    platforms: Mapping[str, Platform],
    posts: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    """给每篇故事补上“各角色视角正文”：优先用 posts 里现成的 Markdown，没有就现场渲染。"""
    for story in stories:
        for role_key in story.roles:
            role = roles.get(role_key)
            if role is None:
                continue
            entry = posts.get((story.id, role_key)) or posts.get(("*", role_key)) or {}
            markdown = _as_text(entry.get("markdown"))
            origin = _as_text(entry.get("markdown_from"))
            if not markdown:
                try:
                    markdown = render_for_role(story.pack, role_key, roles) if story.pack is not None else ""
                    origin = "rendered"
                except Exception as error:  # 单个角色失败不影响整站（RolesError 等都在这里兜住）
                    log.warning("角色 %s × 故事 %s 渲染失败：%s", role_key, story.id, error)
                    markdown, origin = "", "missing"
            copies: list[dict[str, Any]] = []
            for platform_key, text in (entry.get("platforms") or {}).items():
                platform = platforms.get(platform_key)
                copies.append(
                    {
                        "key": platform_key,
                        "name": platform.name if platform else _humanize(platform_key),
                        "emoji": platform.emoji if platform else "",
                        "format": platform.format if platform else "",
                        "language": platform.language if platform else "zh",
                        "text": _as_text(text),
                        "html": markdown_to_html(text),
                    }
                )
            copies.sort(key=lambda item: list(platforms).index(item["key"]) if item["key"] in platforms else 99)
            story.role_bodies[role_key] = {
                "role": role,
                "markdown": markdown,
                "html": markdown_to_html(markdown),
                "origin": origin,
                "words": count_words(markdown),
                "platforms": copies,
            }


# ---------------------------------------------------------------- 模板与渲染
def template_dir(settings: Settings | None = None) -> Path:
    """模板目录：项目 ``web/templates`` 优先，其次包内同路径；两处都没有才报错。"""
    checked: list[Path] = []
    if settings is not None:
        checked.append(Path(settings.output.web) / TEMPLATE_DIR_NAME)
    checked.append(PACKAGE_ROOT / "web" / TEMPLATE_DIR_NAME)
    for candidate in checked:
        if (candidate / "base.html").is_file():
            return candidate
    raise SiteError(
        "找不到站点模板：需要 web/templates/base.html（以及 index.html / story.html / role.html / about.html）。"
        f"已检查：{', '.join(str(item) for item in checked)}"
    )


def _build_env(templates: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates), encoding="utf-8"),
        autoescape=select_autoescape(("html", "htm", "xml")),
        undefined=ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _env_setup(env: Environment) -> Environment:
    env.filters.update({"markdown": markdown_to_html, "words": count_words, "clip": _clip})
    env.globals.update(
        {
            "site_name": SITE_NAME,
            "site_tagline": SITE_TAGLINE,
            "site_tagline_zh": SITE_TAGLINE_ZH,
            "nav_items": NAV_ITEMS,
            "module_labels": MODULE_LABELS,
            "level_hints": LEVEL_HINTS,
            "pipeline_stages": PIPELINE_STAGES,
        }
    )
    return env


#: 首页/关于页要展示的流水线说明（英文为主，中文一句话补充）
PIPELINE_STAGES: tuple[tuple[str, str, str], ...] = (
    ("fetch", "Fetch · 抓取", "Pull RSS/Atom feeds from global English sources into ``output/raw/articles.json``."),
    ("normalize", "Normalize · 清洗聚合", "Filter by age and English ratio, then cluster same-event articles into stories."),
    ("master", "Master · 母版", "One LLM pass per story writes the nine-module pack into ``output/packs``."),
    ("roles", "Roles · 角色", "Rewrite the pack for 12 learner personas, each with its own word budget."),
    ("platforms", "Platforms · 平台", "Fit the persona markdown into six publishing formats."),
    ("tts", "TTS · 配音", "Synthesize free MP3 narration with edge-tts into ``output/audio``."),
    ("site", "Site · 站点", "Render this static site into ``web/`` — no backend, no CDN."),
)


def _stats_for(stories: Sequence[SiteStory], roles: Mapping[str, Role], platforms: Mapping[str, Platform], clips: Sequence[AudioClip]) -> dict[str, Any]:
    return {
        "stories": len(stories),
        "roles": len(roles),
        "platforms": len(platforms),
        "audio": len(clips),
        "words": sum(story.words_total for story in stories),
        "levels": sorted({level for story in stories for level in story.level_labels}),
    }


def _base_context(
    loaded: Settings,
    *,
    root: str,
    kind: str,
    title: str,
    description: str,
    stats: Mapping[str, Any],
    generated: str,
) -> dict[str, Any]:
    return {
        "root": root,
        "page_kind": kind,
        "page_title": title,
        "page_description": description,
        "stats": stats,
        "generated_at": generated,
        "settings": loaded,
        "nav": NAV_ITEMS,
    }


def _last_run(settings: Settings) -> dict[str, Any]:
    """``output/last_run.json``（pipeline 写的运行摘要）：只取扁平的标量项展示。"""
    path = settings.output.last_run
    payload = _read_json(path) if path.is_file() else None
    if not isinstance(payload, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[str(key)] = value
        elif isinstance(value, (list, tuple)):
            out[str(key)] = f"{len(value)}"
        if len(out) >= 10:
            break
    return out


def _copy_static(templates: Path, target: Path) -> list[Path]:
    """把 ``web/templates/static`` 原样复制到 ``web/static``（模板里的 CSS/JS 就是站点资产）。"""
    source = templates / STATIC_DIR_NAME
    if not source.is_dir():
        log.warning("没有 %s，站点将缺少样式与交互", source)
        return []
    written: list[Path] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        destination = target / STATIC_DIR_NAME / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        written.append(destination)
    return written


def _copy_audio(clips: Sequence[AudioClip], target: Path) -> list[Path]:
    """把用到的 mp3 复制进 ``web/audio``，让 ``web/`` 目录可以整包部署。"""
    directory = target / AUDIO_DIR_NAME
    written: list[Path] = []
    for clip in clips:
        source = clip.path
        if source is None or not Path(source).is_file():
            continue
        destination = directory / Path(clip.filename).name
        if destination.exists() and destination.stat().st_size == Path(source).stat().st_size:
            written.append(destination)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        written.append(destination)
    return written


def _category_facet(stories: Sequence[SiteStory]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for story in stories:
        counts[story.category] = counts.get(story.category, 0) + 1
    return [
        {"key": key, "count": value, "en": _humanize(key), "zh": CATEGORY_LABELS.get(key, "")}
        for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _write_page(env: Environment, template: str, target: Path, context: Mapping[str, Any]) -> Path:
    rendered = env.get_template(template).render(**context)
    if not rendered.endswith("\n"):
        rendered += "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return target


def _prune_stale(directory: Path, keep: Sequence[Path]) -> list[Path]:
    """删掉上一版遗留、这一版不再产生的 HTML（只动 story/ 与 role/ 两个子目录）。"""
    removed: list[Path] = []
    wanted = {str(path.resolve()) for path in keep}
    for path in sorted(directory.rglob("*.html")):
        if str(path) in wanted or "templates" in path.parts:
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError as error:  # 只影响清理，不影响本次生成
            log.debug("删除旧页面失败 %s：%s", path, error)
    return removed


def build_site(
    settings: Settings | None = None,
    *,
    packs: Sequence[Any] | None = None,
    web_dir: Any = None,
    posts_dir: Any = None,
    audio_dir: Any = None,
    include_audio: bool = True,
    limit: int | None = None,
    prune: bool = True,
) -> list[Path]:
    """把 ``output/`` 的产物渲染成 ``web/`` 静态站点，返回生成（含复制）的文件清单。

    Args:
        settings: 项目配置；``None`` 时自动 :func:`foundry.config.load_settings`。
        packs: 直接给定母版（:class:`~foundry.models.ContentPack` / JSON 路径 / 映射列表），
            不传就读 ``output/packs/*.json``——测试与 dry-run 可以直接喂 fixture。
        web_dir: 覆盖输出目录（默认 ``settings.output.web``）。
        posts_dir / audio_dir: 覆盖角色与音频来源目录。
        include_audio: 关掉就不读也不复制音频（详情页只留文本）。
        limit: 最多渲染几篇故事（``None`` 全量），给 pipeline 冒烟用。
        prune: 是否删掉 ``web/story`` 与 ``web/role`` 里上一版遗留的页面。
    Returns:
        生成文件的绝对路径列表（HTML + CSS/JS + 音频副本），同时打一条 info 日志。
    Raises:
        SiteError: 模板缺失或目标目录不可写。
    """
    loaded = settings if settings is not None else load_settings()
    target = _resolve(loaded, web_dir, str(loaded.output.web))
    templates = template_dir(loaded)
    roles, platforms = _config_tables(loaded)

    raw_packs = list(packs) if packs is not None else load_packs(_resolve(loaded, None, str(loaded.output.packs)))
    stories: list[SiteStory] = []
    for item in raw_packs:
        coerced = _coerce_pack(item)
        if coerced is None:
            continue
        try:
            stories.append(story_from_pack(coerced, roles=roles, settings=loaded))
        except Exception as error:  # 单个母版坏掉不影响整站
            log.warning("母版 %s 组装失败，跳过：%s", getattr(coerced, "story_id", "?"), error)
    stories.sort(key=lambda story: (story.published or datetime.min.replace(tzinfo=timezone.utc), story.id), reverse=True)
    if limit is not None and limit >= 0:
        stories = stories[:limit]

    posts = discover_posts(
        posts_dir,
        settings=loaded,
        role_keys=list(roles),
        platform_keys=list(platforms),
        story_ids=[story.id for story in stories],
    )
    clips = load_audio_clips(loaded, directory=audio_dir, names=_display_names(roles, platforms)) if include_audio else []
    assign_audio(stories, clips)
    attach_role_bodies(stories, roles=roles, platforms=platforms, posts=posts)

    env = _env_setup(_build_env(templates))
    stats = _stats_for(stories, roles, platforms, clips)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    written: list[Path] = []

    index_context = _base_context(
        loaded,
        root="",
        kind="index",
        title=f"{SITE_NAME} · Daily English, levelled for you",
        description="Static daily English site: graded reading, bilingual glosses, vocabulary, listening downloads and quizzes.",
        stats=stats,
        generated=generated,
    )
    index_context.update(
        {
            "stories": stories,
            "featured": stories[0] if stories else None,
            "categories": _category_facet(stories),
            "role_catalog": [
                {
                    "key": key,
                    "name": role.name,
                    "emoji": role.emoji,
                    "tone": role.tone,
                    "levels": list(role.levels),
                    "focus": role.focus,
                    "href": f"role/{_page_name(key)}.html",
                    "count": sum(1 for story in stories if key in story.roles),
                }
                for key, role in roles.items()
            ],
            "clips": clips,
        }
    )
    written.append(_write_page(env, "index.html", target / "index.html", index_context))
    written.append(
        _write_page(
            env,
            "about.html",
            target / "about.html",
            {
                **_base_context(
                    loaded,
                    root="",
                    kind="about",
                    title=f"About · {SITE_NAME}",
                    description="How this static daily-English site is generated from the foundry pipeline.",
                    stats=stats,
                    generated=generated,
                ),
                "roles_table": [
                    {"key": key, "name": role.name, "emoji": role.emoji, "levels": list(role.levels), "tone": role.tone, "focus": role.focus, "word_limit": role.word_limit, "href": f"role/{_page_name(key)}.html", "modules": list(role.modules)}
                    for key, role in roles.items()
                ],
                "platform_table": [
                    {"key": key, "name": platform.name, "emoji": platform.emoji, "format": platform.format, "limit": platform.char_limit, "unit": platform.unit, "language": platform.language, "notes": platform.notes}
                    for key, platform in platforms.items()
                ],
                "sources": [
                    {"name": _as_text(entry.get("name"), _as_text(entry.get("id"), "source")), "category": _as_text(entry.get("category"), FALLBACK_CATEGORY), "url": _as_text(entry.get("url")), "type": _as_text(entry.get("type"), "rss")}
                    for entry in loaded.enabled_sources()
                ],
                "last_run": _last_run(loaded),
                "clips": clips,
                "audio_dir": loaded.rel(loaded.output.audio),
                "packs_dir": loaded.rel(loaded.output.packs),
                "posts_dir": loaded.rel(loaded.output.posts),
            },
        )
    )

    pages: list[Path] = []
    for position, story in enumerate(stories):
        context = {
            **_base_context(
                loaded,
                root="../",
                kind="story",
                title=f"{story.title} · {SITE_NAME}",
                description=_clip(story.summary or story.title, 200),
                stats=stats,
                generated=generated,
            ),
            "story": story,
            "prev": stories[position - 1] if position else None,
            "next": stories[position + 1] if position + 1 < len(stories) else None,
            "more": [item for item in stories if item.id != story.id][:6],
            "role_catalog": {key: role for key, role in roles.items() if key in story.roles},
        }
        pages.append(_write_page(env, "story.html", target / "story" / f"{_page_name(story.id)}.html", context))
    written.extend(pages)

    role_pages: list[Path] = []
    for key, role in roles.items():
        picked = [story for story in stories if key in story.roles]
        others = [story for story in stories if key not in story.roles]
        context = {
            **_base_context(
                loaded,
                root="../",
                kind="role",
                title=f"{role.name or key} · {SITE_NAME}",
                description=_clip(role.tone or f"Daily English for {key}", 200),
                stats=stats,
                generated=generated,
            ),
            "role": role,
            "role_key": key,
            "stories": picked,
            "others": others[:12],
            "scores": {story.id: round(story.role_scores.get(key, 0.0), 2) for story in picked},
            "platforms_table": [
                {"key": platform_key, "name": platform.name, "emoji": platform.emoji, "format": platform.format, "limit": platform.char_limit}
                for platform_key, platform in platforms.items()
            ],
            "categories": _category_facet(stories),
        }
        role_pages.append(_write_page(env, "role.html", target / "role" / f"{_page_name(key)}.html", context))
    written.extend(role_pages)

    written.extend(_copy_static(templates, target))
    written.extend(_copy_audio(clips, target))

    if prune:
        removed = _prune_stale(target / "story", pages) + _prune_stale(target / "role", role_pages)
        if removed:
            log.info("清理过期页面：%s 个", len(removed))
    if not stories:
        log.warning("没有可用母版：%s —— index.html 仍是空的站点骨架", loaded.rel(loaded.output.packs))

    files = sorted(dict.fromkeys(path for path in written if Path(path).exists()))
    log.info("站点已生成：%s 个文件 -> %s", len(files), loaded.rel(target))
    return files


# ---------------------------------------------------------------- 命令行
def _main(argv: Sequence[str] | None = None) -> int:
    """渲染站点并打印文件清单：``python -m foundry.site``（可加 ``--packs`` / ``--limit``）"""
    parser = argparse.ArgumentParser(description="把 output/ 产物渲染成 web/ 静态站点（Jinja2，纯静态、无后端、无 CDN）")
    parser.add_argument("--packs", metavar="PATH", action="append", default=[], help="只用这些母版 JSON（可重复；默认读 output/packs/*.json）")
    parser.add_argument("--out", metavar="DIR", help="站点输出目录（默认 settings 的 paths.web）")
    parser.add_argument("--posts", metavar="DIR", help="角色/平台文案目录（默认 output/posts）")
    parser.add_argument("--audio", metavar="DIR", help="音频目录（默认 output/audio）")
    parser.add_argument("--no-audio", action="store_true", help="不读取也不复制音频")
    parser.add_argument("--limit", type=int, default=None, help="最多渲染几篇故事（默认全部）")
    parser.add_argument("--no-prune", action="store_true", help="保留上一版遗留的 story/role 页面")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    try:
        files = build_site(
            settings,
            packs=args.packs or None,
            web_dir=args.out,
            posts_dir=args.posts,
            audio_dir=args.audio,
            include_audio=not args.no_audio,
            limit=args.limit,
            prune=not args.no_prune,
        )
    except SiteError as error:
        print(f"站点生成失败：{error}", file=sys.stderr)
        return 1
    if not files:
        print("没有生成任何文件", file=sys.stderr)
        return 1
    print(f"共生成 {len(files)} 个文件（站点目录 {settings.rel(_resolve(settings, args.out, str(settings.output.web)))}）")
    for path in files:
        print(f"  {settings.rel(path)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

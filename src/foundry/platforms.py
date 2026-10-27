"""角色 Markdown → 6 个平台的发布文案（配置在 ``config/platforms.yaml``）。

分工与约定：
    * 输入是 :func:`foundry.roles.render_for_role` 产出的角色 Markdown + ``meta``（标题/链接/来源等），
      输出是一条可直接发布的平台文案；不联网、不调 LLM、纯函数（同样输入永远同样输出）。
    * 平台字段（``platforms.yaml``）：约定 8 项 ``key`` / ``name`` / ``format`` / ``title_rule`` /
      ``char_limit`` / ``emoji`` / ``hashtags`` / ``notes``，另有 3 项扩展：``language``（``zh`` =
      中文框架 + 英文学习内容；``en`` = 全英文，绝不漏出中文注释）、``unit``（``chars`` 按字符计，
      换行不算；``words`` 按词计）与 ``char_min``（长文下限，只作档位目标，不硬凑废话）。
    * 长度是**硬约束**：短平台（微博/X/口播/小红书）用 :class:`_Packer` 在预算内逐行装配——
      话题标签先行预留（永不被截），放不下先砍词表、再降正文，最后才截断必需行；长平台
      （公众号/LinkedIn）按 detail 逐档降级（去例句 → 减词数 → 砍章节 → 截正文），仍超就按
      行/词边界硬截。渲染器同时返回“这档有没有丢内容”，:func:`_render_piece` 优先选
      “装得下且没丢”的最高档，验收口径永远是 :meth:`Platform.fits`。
    * 内容只来自角色 Markdown（标题、英文正文、词汇表、中文背景、长难句、口语题、写作题、收尾任务）
      与 ``meta``（``title`` / ``url`` / ``source`` / ``date`` / ``level`` / ``story_id`` 覆盖同名解析值），
      中文框架句是固定模板——离线渲染，不翻译、不改写英文内容。

用法::

    from foundry.roles import render_for_role
    from foundry.platforms import load_platforms, render_for_platform

    markdown = render_for_role(pack, "business")
    weibo = render_for_platform(markdown, {"url": "https://example.com/a"}, "weibo")

手工看一眼::

    python -m foundry.platforms --list
    python -m foundry.platforms --pack tests/fixtures/sample_pack.json --role business
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from foundry.config import Settings, find_project_root
from foundry.roles import count_words

__all__ = [
    "EXPECTED_PLATFORMS",
    "PLATFORM_LANGUAGES",
    "PLATFORM_UNITS",
    "PLATFORMS_FILE_NAME",
    "Platform",
    "PlatformsError",
    "count_chars",
    "load_platforms",
    "platform_for",
    "platforms_path",
    "render_all_platforms",
    "render_for_platform",
]

log = logging.getLogger("foundry.platforms")

#: 平台配置文件名（放在项目根 config/ 下）
PLATFORMS_FILE_NAME = "platforms.yaml"
#: 内置的 6 个平台 key（顺序 = 渲染顺序；缺失只告警，不报错）
EXPECTED_PLATFORMS: tuple[str, ...] = (
    "xiaohongshu",
    "wechat_mp",
    "shortvideo",
    "weibo",
    "x",
    "linkedin",
)
PLATFORM_LANGUAGES: tuple[str, ...] = ("zh", "en")
PLATFORM_UNITS: tuple[str, ...] = ("chars", "words")
DEFAULT_CHAR_LIMIT = 600

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019\-][A-Za-z0-9]+)*|[0-9][0-9.,]*")
_SENTENCE_RE = re.compile(r"[^.!?。！？\n]+[.!?。！？]*")
_PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")
#: 小红书词卡序号（keycap 是多码点，按 Python len 保守计数，不会因此超预算）
_DIGIT_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
_CN_NUMBERS = "一二三四五六七八九十"
_STOPWORDS = frozenset(
    "a an the of and or to in on for with over at by from as is are was were be been being "
    "this that it its their our your you new says said say against into about after before "
    "up down out off then than when while who whom which what how why".split()
)


class PlatformsError(RuntimeError):
    """平台配置缺失或非法（字段写错、unit 不认识、char_limit 不是正整数）。"""


# ---------------------------------------------------------------- 小工具
def _as_text(value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return ""
    return str(value).strip()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [text for text in (_as_text(item) for item in value) if text]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", _as_text(value)).strip()


def _positive_int(value: Any, default: int, source: Path, key: str, field: str) -> int:
    """正整数旋钮：没写用默认值；写了就必须是正整数（"abc"、0、-3 直接报错，不静默回退）。"""
    if value is None or _as_text(value) == "":
        return default
    text = _as_text(value)
    if not re.fullmatch(r"\+?\d+", text) or int(text.lstrip("+")) <= 0:
        raise PlatformsError(f"{source}：平台 {key} 的 {field} 必须是正整数，实际为 {value!r}")
    return int(text.lstrip("+"))


def _norm_platform_key(value: Any) -> str:
    """平台 key 归一：小写、空格/连字符都当下划线（``WeChat-MP`` -> ``wechat_mp``）。"""
    return re.sub(r"[\s\-]+", "_", _as_text(value).lower().strip("_"))


def count_chars(text: str) -> int:
    """字符数：换行不计，其余逐字符（中文一字 / 英文一字母 / 标点 / emoji 各算 1 个 len）。"""
    return sum(len(line) for line in text.splitlines())


def _trim_words(text: str, limit: int) -> str:
    """按句累加到 ``limit`` 词，超出补 ``…``（英文内容专用，不劈开单词）。"""
    text = _clean(text)
    if not text or limit <= 0 or count_words(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for raw in _SENTENCE_RE.findall(text):
        sentence = raw.strip()
        if not sentence:
            continue
        cost = count_words(sentence)
        if kept and used + cost > limit:
            break
        kept.append(sentence)
        used += cost
    result = " ".join(kept)
    if count_words(result) > limit:
        result = _cut_words(result, limit, marker="")
    return result.rstrip(" .,;:!?") + " …"


def _cut_words(text: str, limit: int, marker: str = " …") -> str:
    """硬截断到 ``limit`` 个词：按词边界回退，并用 :func:`count_words` 复核，
    保证截出来的词数与计数口径完全一致（两套正则可能差半个连字符）。"""
    if limit <= 0:
        return ""
    if count_words(text) <= limit:
        return text
    spans = [match.span() for match in _WORD_TOKEN_RE.finditer(text)]
    keep = min(limit, len(spans))
    while keep > 0:
        candidate = text[: spans[keep - 1][1]].rstrip(" ,;:-") + marker
        if count_words(candidate) <= limit:
            return candidate
        keep -= 1
    return ""


def _trim_chars(text: str, budget: int) -> str:
    """按字符预算截一行：不劈开英文单词，尾部补 ``…``。"""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    cut = text[:budget]
    boundary = text[budget] if budget < len(text) else ""
    if boundary and re.fullmatch(r"[A-Za-z0-9'\u2019\-]", boundary) and re.search(
        r"[A-Za-z0-9'\u2019\-]$", cut
    ):
        cut = re.sub(r"[A-Za-z0-9'\u2019\-]+$", "", cut)  # 切在单词中间 → 退回词首
    cut = cut.rstrip(" ，,、;；:：—-")
    if budget > 1 and cut:
        cut = cut[: budget - 1].rstrip() + "…"
    return cut


def _en_sentences(text: str) -> list[str]:
    """取英文句（丢弃含中文的片段），供全英文平台使用。"""
    out: list[str] = []
    for raw in _SENTENCE_RE.findall(_as_text(text)):
        sentence = _clean(raw).strip("*").strip()
        if not sentence or _CJK_RE.search(sentence):
            continue
        out.append(sentence)
    return out


def _camel(*words: str) -> str:
    parts = [re.sub(r"[^0-9A-Za-z]", "", _as_text(word)) for word in words]
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def _cn_number(index: int) -> str:
    return _CN_NUMBERS[index - 1] if 1 <= index <= len(_CN_NUMBERS) else str(index)


def _join_and(items: Sequence[str]) -> str:
    """a / a and b / a, b and c（英文列表别写出 "a, b." 这种机翻味）。"""
    parts = [item for item in (_clean(value) for value in items) if item]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# ---------------------------------------------------------------- 平台
@dataclass(frozen=True)
class Platform:
    """一个平台：内容形态 + 标题规则 + 长度硬上限 + 标签池。"""

    key: str
    name: str
    format: str = ""
    title_rule: str = ""
    char_limit: int = DEFAULT_CHAR_LIMIT
    emoji: str = ""
    hashtags: tuple[str, ...] = ()
    notes: str = ""
    language: str = "zh"
    unit: str = "chars"
    char_min: int = 1

    @property
    def is_chinese(self) -> bool:
        return self.language == "zh"

    def count(self, text: str) -> int:
        """按平台口径计数：``chars`` 数字符（换行除外），``words`` 数词。"""
        return count_words(text) if self.unit == "words" else count_chars(text)

    def fits(self, text: str) -> bool:
        """文案是否满足该平台 ``char_limit`` 硬上限（验收口径）。"""
        return 0 < self.count(text) <= self.char_limit

    def summary(self) -> str:
        unit_name = "词" if self.unit == "words" else "字"
        floor = f"（≥{self.char_min}）" if self.char_min > 1 else ""
        tags = ",".join(self.hashtags) if self.hashtags else "-"
        return (
            f"{self.key:<13}{self.name:<10}[{self.language}/{self.unit}] "
            f"≤{self.char_limit}{unit_name}{floor:<7} emoji={self.emoji or '-'} tags={tags}"
        )


def platforms_path(path: str | Path | None = None, settings: Settings | None = None) -> Path:
    """定位平台配置：显式 path > ``settings.root`` > 自动发现的项目根，相对路径按项目根解析。"""
    if path is not None:
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate
        root = settings.root if settings is not None else find_project_root()
        return (root / candidate).resolve()
    root = settings.root if settings is not None else find_project_root()
    return (root / "config" / PLATFORMS_FILE_NAME).resolve()


def _coerce_entries(payload: Any, source: Path) -> list[Mapping[str, Any]]:
    """把 yaml 里的 platforms 段取成条目列表（支持 ``platforms: {key: {...}}`` 与 ``platforms: [...]``）。"""
    if isinstance(payload, Mapping) and "platforms" in payload:
        inner = payload.get("platforms")
        if not isinstance(inner, (Mapping, list, tuple)):
            raise PlatformsError(f"{source} 的 platforms: 必须是映射或列表，实际为 {type(inner).__name__}")
        payload = inner
    if isinstance(payload, Mapping):
        entries = []
        for key, value in payload.items():
            item = dict(value) if isinstance(value, Mapping) else {"name": value}
            item.setdefault("key", key)
            entries.append(item)
        return entries
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    raise PlatformsError(f"{source} 的 platforms: 必须是映射或列表，实际为 {type(payload).__name__}")


def _build_platform(entry: Mapping[str, Any], source: Path) -> Platform:
    """校验并构造一个 Platform：8 个约定字段 + language / unit / char_min。"""
    key = _norm_platform_key(entry.get("key") or entry.get("id") or entry.get("name"))
    if not key:
        raise PlatformsError(f"{source}：有条目缺 key/id/name，无法确定平台标识：{entry!r}")

    raw_hashtags = entry.get("hashtags")
    if raw_hashtags is not None and not isinstance(raw_hashtags, (list, tuple, str)):
        raise PlatformsError(f"{source}：平台 {key} 的 hashtags 必须是列表")

    limit = _positive_int(entry.get("char_limit"), DEFAULT_CHAR_LIMIT, source, key, "char_limit")

    unit = _norm_platform_key(entry.get("unit") or "chars")
    if unit not in PLATFORM_UNITS:
        raise PlatformsError(f"{source}：平台 {key} 的 unit 只能是 {PLATFORM_UNITS} 之一，实际为 {entry.get('unit')!r}")

    language = _as_text(entry.get("language") or "zh").lower()
    if language not in PLATFORM_LANGUAGES:
        raise PlatformsError(
            f"{source}：平台 {key} 的 language 只能是 {PLATFORM_LANGUAGES} 之一，实际为 {entry.get('language')!r}"
        )

    char_min = _positive_int(entry.get("char_min"), 1, source, key, "char_min")
    if char_min > limit:
        raise PlatformsError(f"{source}：平台 {key} 的 char_min（{char_min}）不能大于 char_limit（{limit}）")

    return Platform(
        key=key,
        name=_as_text(entry.get("name")) or key,
        format=_as_text(entry.get("format")),
        title_rule=_as_text(entry.get("title_rule")),
        char_limit=limit,
        emoji=_as_text(entry.get("emoji")),
        hashtags=tuple(_strings(raw_hashtags)),
        notes=_as_text(entry.get("notes")),
        language=language,
        unit=unit,
        char_min=char_min,
    )


def load_platforms(path: str | Path | None = None, settings: Settings | None = None) -> dict[str, Platform]:
    """读取平台配置，返回 ``{key: Platform}``（顺序 = 文件里的顺序）。

    Args:
        path: 显式指定平台 yaml；默认 ``<项目根>/config/platforms.yaml``。
        settings: 已加载的配置（用来取项目根），不传则自动定位项目根。
    Raises:
        PlatformsError: 文件不存在、顶层不是映射、字段非法。
    """
    source = platforms_path(path, settings)
    if not source.is_file():
        raise PlatformsError(
            f"找不到平台配置：{source}。请创建该文件并写入 platforms: 段（6 个平台见 prompts/build/05_platforms.md）。"
        )
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:  # 缩进/引号错误要说清是哪个文件
        raise PlatformsError(f"{source} 不是合法 YAML：{error}") from error
    if payload is None:
        raise PlatformsError(f"{source} 是空的，至少需要 platforms: 段")

    platforms: dict[str, Platform] = {}
    for entry in _coerce_entries(payload, source):
        platform = _build_platform(entry, source)
        if platform.key in platforms:
            raise PlatformsError(f"{source}：平台 key 重复：{platform.key}")
        platforms[platform.key] = platform
    if not platforms:
        raise PlatformsError(f"{source} 里一个平台都没有（platforms: 段为空）")

    missing = [key for key in EXPECTED_PLATFORMS if key not in platforms]
    if missing:
        log.warning("%s 缺少约定平台：%s", source.name, ", ".join(missing))
    log.debug("已加载 %s 个平台：%s", len(platforms), ", ".join(platforms))
    return platforms


def platform_for(platform_key: str, platforms: Mapping[str, Platform] | None = None) -> Platform:
    """按 key 取平台（大小写/连字符不敏感）；找不到就报错并列出可用值。"""
    table = platforms if platforms is not None else load_platforms()
    key = _norm_platform_key(platform_key)
    if key not in table:
        raise PlatformsError(f"没有这个平台：{platform_key!r}（可用：{', '.join(table)}）")
    return table[key]


# ---------------------------------------------------------------- 角色 Markdown 解析
@dataclass
class _Digest:
    """角色 Markdown 的结构化快照；缺哪个字段就空着，渲染只会少给一个板块，不会崩。"""

    title: str = ""
    role_name: str = ""
    role_key: str = ""
    level: str = ""
    tone: str = ""
    focus: str = ""
    hits: tuple[str, ...] = ()
    story_id: str = ""
    url: str = ""
    source: str = ""
    date: str = ""
    body: str = ""
    background_zh: str = ""
    bilingual: tuple[tuple[str, str], ...] = ()
    vocabulary: tuple[dict[str, str], ...] = ()
    grammar: tuple[dict[str, str], ...] = ()
    listening: str = ""
    questions: tuple[str, ...] = ()
    roleplay: dict[str, Any] = field(default_factory=dict)
    writing_topic: str = ""
    essay: str = ""
    quiz: tuple[str, ...] = ()
    cta: str = ""

    # ---- 派生取值 ----
    def words(self, count: int) -> list[dict[str, str]]:
        return [dict(item) for item in self.vocabulary[: max(0, count)]]

    def glosses(self, count: int) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for item in self.vocabulary[: max(0, count)]:
            word = _as_text(item.get("word"))
            if not word:
                continue
            gloss = _clean(re.split(r"[；;，,/]", _as_text(item.get("zh")))[0])
            out.append((word, gloss or _clean(_as_text(item.get("en_def")))))
        return out

    def english_body(self, max_words: int = 200) -> str:
        """英文正文：优先 ``## 正文`` 段，缺失则用双语句 / 长难句兜底（全英文平台用）。"""
        text = self.body or " ".join(en for en, _ in self.bilingual if en)
        if not text:
            text = ". ".join(item.get("sentence", "") for item in self.grammar if item.get("sentence"))
        return _clean(_trim_words(text, max_words))

    def summary_zh(self, char_limit: int) -> str:
        """中文一句话串讲：优先背景速览，其次双语译文拼接（口播/短文案用）。"""
        text = _clean(self.background_zh or " ".join(zh for _, zh in self.bilingual if zh))
        if len(text) <= char_limit:
            return text
        cut = text[:char_limit]
        return cut.rstrip(" ，,。;；:：") + "。"

    def number_en(self) -> str:
        """标题/正文里最扎眼的英文数字短语（如 ``430 million euros``），英文钩子用。"""
        match = re.search(
            r"\b\d[\d,]*(?:\.\d+)?\s*(?:trillion|billion|million|thousand)?"
            r"(?:\s*(?:euros?|dollars?|pounds?|yuan|renminbi))?\b",
            f"{self.title} {self.body}",
            re.I,
        )
        return _clean(match.group(0)) if match else ""

    def number_zh(self) -> str:
        """把英文数字短语换成中文（430 million euros → 4.3亿欧元）；找不到返回空串。"""
        raw = self.number_en()
        if not raw:
            return ""
        match = re.match(r"([\d,.]+)\s*(trillion|billion|million|thousand)?(.*)", raw, re.I)
        if not match:
            return ""
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            return ""
        scale = (match.group(2) or "").lower()
        factor = {"trillion": 1e12, "billion": 1e9, "million": 1e6, "thousand": 1e3}.get(scale, 1.0)
        total = value * factor
        currency = {
            "euro": "欧元", "euros": "欧元", "dollar": "美元", "dollars": "美元",
            "pound": "英镑", "pounds": "英镑", "yuan": "元", "renminbi": "人民币",
        }.get((match.group(3) or "").strip().lower(), "")
        if total >= 1e8:
            head = f"{total / 1e8:.1f}".removesuffix(".0") + "亿"
        elif total >= 1e4:
            head = f"{total / 1e4:.1f}".removesuffix(".0") + "万"
        else:
            small = {"trillion": "万亿", "billion": "十亿", "million": "百万", "thousand": "千"}.get(scale, "")
            head = f"{value:g}{small}"
        return head + currency


_SECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("长难句", "grammar"),
    ("正文", "body"),
    ("背景", "background"),
    ("逐句", "bilingual"),
    ("词汇", "vocabulary"),
    ("听力", "listening"),
    ("开口", "speaking"),
    ("写作", "writing"),
    ("练习", "quiz"),
    ("收尾", "cta"),
)


def _classify_section(heading: str) -> str:
    for marker, name in _SECTION_PATTERNS:
        if marker in heading:
            return name
    return ""


def _is_boiler(line: str) -> bool:
    """跨板块通用噪声：引用提示、折叠标签、分隔线、页脚署名行。"""
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith(">")
        or stripped.startswith("<")
        or stripped == "---"
        or stripped.startswith("*EnglishFoundry")
        or stripped.startswith("*配音建议")
    )


def _paragraph(bucket: Sequence[str]) -> str:
    lines = [line.strip().replace("**", "") for line in bucket if not _is_boiler(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _parse_vocabulary(bucket: Sequence[str]) -> list[dict[str, str]]:
    """解析词汇表（表头决定列含义，列数随 roles 的降级档位变化）。"""
    index: dict[str, int] = {}
    rows: list[dict[str, str]] = []
    for line in bucket:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in _PIPE_SPLIT_RE.split(stripped)[1:-1]]
        if not cells or all(re.fullmatch(r":?-{2,}:?", cell or "-") for cell in cells):
            continue
        if "单词" in cells and "中文" in cells:
            labels = {"单词": "word", "词性": "pos", "中文": "zh", "英文释义": "en_def", "例句": "example"}
            index = {
                name: position
                for position, cell in enumerate(cells)
                for label, name in labels.items()
                if cell == label
            }
            continue
        if not index:
            continue
        item = {name: (cells[position] if position < len(cells) else "") for name, position in index.items()}
        item["word"] = re.sub(r"\*+", "", _as_text(item.get("word"))).replace("⭐", "").strip()
        if item.get("word"):
            rows.append(item)
    return rows


def _parse_grammar(bucket: Sequence[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in bucket:
        stripped = line.strip()
        match = re.match(r"^\*\*\d+[.、]\s*(.+?)\*\*$", stripped)
        if match:
            current = {"sentence": _clean(match.group(1))}
            items.append(current)
            continue
        if current is None:
            continue
        if stripped.startswith("- 语法点："):
            current["point"] = _clean(stripped.split("：", 1)[1])
        elif stripped.startswith("- 讲解："):
            current["explanation"] = _clean(stripped.split("：", 1)[1])
    return items


def _parse_speaking(bucket: Sequence[str]) -> tuple[list[str], dict[str, Any]]:
    questions: list[str] = []
    roleplay: dict[str, Any] = {}
    for line in bucket:
        stripped = line.strip()
        match = re.match(r"^\*\*角色扮演(?:（[^）]*）)?\*\*：(.+)$", stripped)
        if match:
            roleplay["title"] = _clean(match.group(1))
            continue
        if stripped.startswith("- 场景："):
            roleplay["scenario"] = _clean(stripped.split("：", 1)[1])
            continue
        if stripped.startswith("- 角色："):
            roles = roleplay.setdefault("roles", [])
            if isinstance(roles, list):
                roles.append(_clean(stripped.split("：", 1)[1]))
            continue
        if stripped.startswith("- 任务："):
            roleplay["task"] = _clean(stripped.split("：", 1)[1])
            continue
        if stripped.startswith("-"):
            continue
        match = re.match(r"^\d+[.、]\s+(.+)$", stripped)
        if match:
            questions.append(_clean(match.group(1)))
    return questions, roleplay


def _parse_writing(bucket: Sequence[str]) -> tuple[str, str]:
    topic = ""
    essay: list[str] = []
    for line in bucket:
        stripped = line.strip()
        if stripped.startswith("**题目**"):
            topic = _clean(stripped.split("：", 1)[1]) if "：" in stripped else ""
            continue
        if stripped and not _is_boiler(stripped):
            essay.append(stripped.replace("**", ""))
    return topic, _clean(" ".join(essay))


def _parse_quiz(bucket: Sequence[str]) -> tuple[list[str], dict[int, str]]:
    questions: list[str] = []
    letters: dict[int, str] = {}
    in_details = False
    for line in bucket:
        stripped = line.strip()
        if stripped.startswith("<details"):
            in_details = True
            continue
        if stripped.startswith("</details"):
            in_details = False
            continue
        if in_details:
            match = re.match(r"^(\d+)\.\s*\*\*([A-Z])\*\*", stripped)
            if match:
                letters[int(match.group(1))] = match.group(2)
                continue
            if stripped.startswith("答案速查："):
                for number, letter in re.findall(r"(\d+)\.([A-Z?])", stripped):
                    letters.setdefault(int(number), letter)
            continue
        if stripped.startswith("-") or _is_boiler(stripped):
            continue
        match = re.match(r"^\*\*(\d+)\.?\*\*\s*(.+)$", stripped) or re.match(r"^\d+[.、]\s+(.+)$", stripped)
        if match:
            questions.append(_clean(match.groups()[-1]))
    return questions, letters


def _digest_of(role_markdown: str) -> _Digest:
    """把角色 Markdown 拆成结构化快照（解析失败的最多后果是某板块缺失）。"""
    digest = _Digest()
    if not role_markdown or not role_markdown.strip():
        return digest

    sections: list[tuple[str, list[str]]] = []
    heading = ""
    in_header = True
    for line in role_markdown.replace("\r\n", "\n").split("\n"):
        if line.startswith("# ") and not digest.title:
            digest.title = _clean(re.sub(r"^[^\w\u4e00-\u9fff]+", "", line[2:]))
            continue
        if line.startswith("## "):
            in_header = False
            heading = _classify_section(line[3:])
            if heading:
                sections.append((heading, []))
            continue
        if in_header and line.startswith(">"):
            match = re.match(r"^>\s*角色：\*\*(.+?)\*\*（`([\w.\-]+)`）", line)
            if match:
                digest.role_name, digest.role_key = _clean(match.group(1)), match.group(2)
                continue
            match = re.match(r"^>\s*难度：([A-C][1-2])", line)
            if match:
                digest.level = match.group(1)
                continue
            if line.startswith("> 讲解风格："):
                digest.tone = _clean(line.split("：", 1)[1])
                continue
            if line.startswith("> 本篇重点："):
                digest.focus = _clean(line.split("：", 1)[1])
                continue
            match = re.search(r"题材契合：.*→\s*(.+)$", line)
            if match:
                digest.hits = tuple(_clean(part) for part in match.group(1).split("、") if _clean(part))
            continue
        if heading and sections:
            sections[-1][1].append(line)

    first: dict[str, list[str]] = {}
    for name, lines in sections:
        first.setdefault(name, lines)

    body_lines = [line for line in first.get("body", []) if _clean(line.replace("**", "")) != digest.title]
    digest.body = _paragraph(body_lines)
    digest.background_zh = _paragraph(first.get("background", []))
    digest.listening = _paragraph(first.get("listening", []))
    digest.cta = _paragraph(first.get("cta", []))
    digest.vocabulary = tuple(_parse_vocabulary(first.get("vocabulary", [])))
    digest.grammar = tuple(_parse_grammar(first.get("grammar", [])))
    digest.questions, digest.roleplay = _parse_speaking(first.get("speaking", []))
    digest.writing_topic, digest.essay = _parse_writing(first.get("writing", []))
    quiz_questions, quiz_letters = _parse_quiz(first.get("quiz", []))
    digest.quiz = tuple(
        f"{_trim_words(question, 14)}（答案 {quiz_letters[position]}）"
        if quiz_letters.get(position)
        else _trim_words(question, 14)
        for position, question in enumerate(quiz_questions, start=1)
    )

    pairs: list[tuple[str, str]] = []
    for line in first.get("bilingual", []):
        stripped = line.strip()
        if stripped.startswith("译：") or stripped.startswith("- 译："):
            if pairs:
                en, _ = pairs[-1]
                pairs[-1] = (en, _clean(stripped.split("：", 1)[1]))
        elif stripped.startswith("- "):
            en = _clean(stripped[2:])
            if en and not en.startswith("译"):
                pairs.append((en, ""))
    digest.bilingual = tuple(pairs)

    match = re.search(r"母版\s*`([^`]+)`", role_markdown)
    if match:
        digest.story_id = match.group(1).strip()
    return digest


_META_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "headline"),
    "url": ("url", "link", "link_url"),
    "source": ("source", "site", "publisher"),
    "date": ("date", "day", "published", "publish_date"),
    "level": ("level", "difficulty", "cefr"),
    "story_id": ("story_id", "pack", "pack_id"),
    "role_name": ("role", "role_name"),
}


def _apply_meta(digest: _Digest, meta: Mapping[str, Any] | None) -> _Digest:
    """meta 里给了同名信息就用 meta 的（大小写不敏感，其余不猜）。"""
    if not meta:
        return digest
    lowered = {str(key).lower(): value for key, value in meta.items()}
    for attribute, names in _META_ALIASES.items():
        for name in names:
            value = _clean(lowered.get(name))
            if value:
                setattr(digest, attribute, value)
                break
    hits = lowered.get("tags") or lowered.get("hits")
    if hits:
        digest.hits = tuple(_strings(hits))
    return digest


# ---------------------------------------------------------------- 拼装配（短平台预算内组装）
class _Packer:
    """按平台计数逐行装配：required 行放不下就截断（截过的算“丢内容”），其余行放不下直接丢。

    ``reserve`` 是给稍后才追加的话题标签/词表预留的预算：调用 :meth:`release` 后，
    预留额度自动让位给这些行本身，避免“先扣一遍预算、追加时再扣一遍”的双重计费。
    """

    def __init__(self, platform: Platform, reserve: str = "") -> None:
        self.platform = platform
        self.reserve_count = platform.count(reserve) if reserve else 0
        self.lines: list[str] = []
        self.dropped: list[str] = []
        self.truncated = False

    def used(self) -> int:
        return self.platform.count("\n".join(self.lines))

    def room(self, extra_reserve: int = 0) -> int:
        return max(0, self.platform.char_limit - self.used() - self.reserve_count - extra_reserve)

    def line(self, text: str = "", *, required: bool = False, extra_reserve: int = 0, quiet: bool = False) -> bool:
        if not text:
            if self.lines and self.lines[-1] != "":
                self.lines.append("")
            return True
        room = self.room(extra_reserve)
        if self.platform.count(text) <= room:
            self.lines.append(text)
            return True
        if required:
            trimmed = (
                _trim_chars(text, room) if self.platform.unit == "chars" else _cut_words(text, room, marker="…")
            )
            if trimmed:
                self.lines.append(trimmed)
                self.truncated = True
                return True
        elif not quiet:
            self.dropped.append(text)
        return False

    def release(self) -> None:
        self.reserve_count = 0

    @property
    def satisfied(self) -> bool:
        """这档装配没有丢行也没有截行 —— 内容完整。"""
        return not self.dropped and not self.truncated

    def text(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


def _tag_line(digest: _Digest, platform: Platform, limit: int, pair: bool = False) -> str:
    """话题标签行：英文平台 = 1 个动态（标题实义词拼接）+ 固定池；中文平台 = 固定池 + 命中关键词。

    ``pair=True`` 用微博式 ``#话题#``，否则用 ``#话题``（小红书/公众号文末风格）。
    """
    template = "#{tag}#" if pair else "#{tag}"
    tags: list[str] = []
    if not platform.is_chinese:
        tokens = [word for word in re.findall(r"[A-Za-z]{3,}", _as_text(digest.title)) if word.lower() not in _STOPWORDS]
        if len(tokens) >= 2:
            tags.append(template.format(tag=_camel(tokens[0], tokens[1])))
        elif tokens:
            tags.append(template.format(tag=_camel(tokens[0])))
    for base in platform.hashtags:
        candidate = template.format(tag=_as_text(base))
        if candidate not in tags:
            tags.append(candidate)
        if len(tags) >= limit:
            break
    if platform.is_chinese:
        for hit in digest.hits:
            if len(tags) >= limit:
                break
            candidate = template.format(tag=_camel(hit))
            if candidate not in tags:
                tags.append(candidate)
    return " ".join(tags[:limit])


def _headline_hook(digest: _Digest, platform: Platform, count: int) -> str:
    number = digest.number_zh()
    if number and count:
        return f"{platform.emoji} {number}！一条外刊新闻带你吃透 {count} 个高频词"
    if count:
        return f"{platform.emoji} 今天的英文头条，{count} 个关键词就够看懂"
    return f"{platform.emoji} 今天的英文头条，值得花 3 分钟精读"


# ---------------------------------------------------------------- 各平台 builder（返回 (文案, 内容是否完整)）
def _build_xiaohongshu(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """小红书：钩子标题 + 英文头条 + 一句话事件 + 分点词卡 + 互动 + 话题标签，约 600 字。

    词卡逐张往里装：主行装不下就停（序号保持连续，表头/钩子的张数跟着改写），
    例句装不下只是静默不给例句——降级发生在“内容多少”，而不是把整块词卡丢掉。
    """
    word_count = (5, 4, 3, 2, 2)[detail]
    with_example = detail <= 1
    with_question = detail == 0
    glosses = digest.glosses(word_count)
    tags = _tag_line(digest, platform, 6)
    event = digest.english_body(max_words=(28, 22, 16, 12, 12)[detail])

    packer = _Packer(platform, reserve=tags)
    packer.line(_headline_hook(digest, platform, len(glosses)), required=True)
    packer.line("")
    packer.line(f"📌 今日英文头条｜{digest.title}", required=True)
    packer.line("")
    if event:
        packer.line(f"🗞 一句话事件：{event}")
        packer.line("")
    if glosses:
        header_in = packer.line(f"🧠 词卡直接抄进备忘录（{len(glosses)} 张）")
        header_at = len(packer.lines) - 1
        kept = 0
        for position, item in enumerate(digest.words(len(glosses))):
            digit = _DIGIT_EMOJI[position] if position < len(_DIGIT_EMOJI) else "🔸"
            main = f"{digit} {item.get('word')} {item.get('pos', '')}｜{glosses[position][1]}"
            if not packer.line(main):
                break
            kept += 1
            example = _trim_words(_as_text(item.get("example")), 12) if with_example else ""
            if example:
                packer.line(f"　　原文：{example}", quiet=True)
        if kept:
            packer.line("")
            if header_in:
                packer.lines[header_at] = f"🧠 词卡直接抄进备忘录（{kept} 张）"
            if kept != len(glosses):
                packer.lines[0] = _headline_hook(digest, platform, kept)
        elif header_in:
            del packer.lines[header_at]
    if with_question and digest.questions:
        packer.line(f"💬 敢用英语回答吗：{digest.questions[0]}")
        packer.line("")
    closing = digest.cta or "🎧 完整版带 B1 听力 + 答案，主页合集见，评论区回「词表」自取"
    packer.line(closing)
    packer.release()
    if tags:
        packer.line(tags, required=True)
    return packer.text(), packer.satisfied


def _build_wechat_mp(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """公众号：深度长文，小标题分层：原文 → 中文背景 → 词汇精讲 → 长难句 → 听说 → 写作 → 收尾任务。"""
    word_count = (10, 8, 6, 4, 3)[detail]
    body_words = (200, 160, 160, 120, 90)[detail]
    with_example = detail <= 0
    with_definition = detail <= 1
    with_grammar = detail <= 2
    with_skills = detail <= 1
    with_writing = detail <= 2
    words = digest.words(word_count)

    number = digest.number_zh()
    main_hook = f"{number}！这条英文新闻为什么值得逐句精读？" if number else "今天的英文头条，为什么值得逐句精读？"
    blocks: list[str] = [f"# {platform.emoji} {main_hook}", f"**{digest.title}**"]
    intro_bits = []
    if digest.role_name:
        intro_bits.append(f"角色：{digest.role_name}")
    if digest.level:
        intro_bits.append(f"CEFR {digest.level}")
    if digest.focus:
        intro_bits.append(f"重点：{digest.focus}")
    intro_bits.append(f"核心词 {len(words)} 个")
    blocks.append(
        "> 导读：" + "｜".join(_clean(bit) for bit in intro_bits) + "。建议先读英文原文，再对照中文背景，最后把词卡收进笔记本。"
    )

    sections: list[tuple[str, str]] = []
    body = _clean(digest.english_body(max_words=body_words))
    if body:
        sections.append((f"事件原文（{digest.level or '分级'}）", body))
    background = digest.background_zh or digest.summary_zh(150)
    if background:
        sections.append(("中文背景", background))
    if words:
        entries = []
        for position, item in enumerate(words, start=1):
            gloss = _clean(re.split(r"[；;，,/]", _as_text(item.get("zh")))[0])
            entry = f"### {position}. **{item.get('word')}** {_as_text(item.get('pos'))}｜{gloss}"
            if with_definition and _as_text(item.get("en_def")):
                entry += f"\n- 英文释义：{_clean(item.get('en_def'))}"
            if with_example and _as_text(item.get("example")):
                entry += f"\n- 新闻例句：{_clean(item.get('example'))}"
            entries.append(entry)
        if digest.hits:
            entries.append(f"> ⭐ 命中本篇方向关键词：{'、'.join(digest.hits)}")
        sections.append((f"核心词汇精讲（{len(words)} 个）", "\n\n".join(entries)))
    if with_grammar and digest.grammar:
        entries = []
        for position, item in enumerate(digest.grammar[: 2 if detail <= 1 else 1], start=1):
            entry = f"{position}. {item.get('sentence', '')}"
            if item.get("point"):
                entry += f"\n- 语法点：{item['point']}"
            explanation = item.get("explanation", "")
            if explanation:
                entry += f"\n- 讲解：{_clean(explanation[: 160 if detail == 0 else 80])}"
            entries.append(entry)
        sections.append(("长难句拆解", "\n\n".join(entries)))
    if with_skills and (digest.listening or digest.questions):
        entries = []
        if digest.listening:
            entries.append(f"🎧 听力稿节选：{_trim_words(digest.listening, 50)}")
        if digest.questions:
            entries.append("🗣 开口讨论：" + " ".join(digest.questions[:2]))
        sections.append(("听力与跟读", "\n\n".join(entries)))
    if with_writing and (digest.writing_topic or digest.essay):
        entries = []
        if digest.writing_topic:
            entries.append(f"题目：{_trim_words(digest.writing_topic, 40)}")
        if digest.essay:
            entries.append(f"范文节选：{_trim_words(digest.essay, 60)}")
        sections.append(("写作练笔", "\n\n".join(entries)))
    sections.append(("收尾任务", digest.cta or "把今天的词卡各造一个句子，明早再来复读英文原文。"))

    for position, (name, content) in enumerate(sections, start=1):
        blocks.append(f"## {_cn_number(position)}、{name}\n\n{content}")

    footer_bits = []
    if digest.story_id:
        footer_bits.append(f"素材：母版 `{digest.story_id}`")
    if digest.role_key:
        footer_bits.append(f"角色 `{digest.role_key}`")
    if digest.date:
        footer_bits.append(digest.date)
    footer = "｜".join(footer_bits) or "素材：EnglishFoundry 每日外刊"
    link = f" 阅读原文：{digest.url} ｜" if digest.url else ""
    blocks.append(f"---\n*{footer}，由 EnglishFoundry 自动排版。{link} 觉得有用，点个「在看」收藏这个系列。*")
    return "\n\n".join(blocks) + "\n", True


def _build_shortvideo(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """短视频口播：前 3 秒钩子 → 英文原句跟读 → 中文串讲 → 核心词 → 引导关注，30-60 秒逐字稿。"""
    tags = _tag_line(digest, platform, 3)
    packer = _Packer(platform, reserve=tags)
    number = digest.number_zh()
    if detail >= 3:
        hook = "【3秒钩子】这条英文新闻，一句话 + 两个词，30 秒拿下👇"
    elif number:
        hook = f"【3秒钩子】{number}？这条英文新闻开场就一句，30 秒带你听懂👇"
    else:
        hook = "【3秒钩子】每天 30 秒，一条外刊新闻带你把英语用起来👇"
    packer.line(hook, required=True)
    packer.line("")
    sentence = " ".join(_en_sentences(digest.body)[:1])
    if sentence:
        packer.line(f"【跟读】{_trim_words(sentence, (24, 18, 14, 10, 10)[detail])}")
    zh = digest.summary_zh((60, 45, 35, 25, 25)[detail])
    if zh:
        packer.line(f"【串讲】{zh}")
    glosses = digest.glosses((3, 3, 2, 2, 2)[detail])
    if glosses:
        packer.line("【核心词】" + "；".join(f"{word}（{gloss}）" for word, gloss in glosses))
    packer.line("【结尾】完整词表和音频在主页，评论区回「学习」。明天见👋")
    packer.release()
    if tags:
        packer.line(tags, required=True)
    return packer.text(), packer.satisfied


def _build_weibo(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """微博：#话题# + 一句话 + 英文关键词（中文），含标点与链接 ≤140 字；话题标签永不被截。"""
    tags = _tag_line(digest, platform, 2, pair=True) or "#英语# #外刊#"
    link = f" 详情→{digest.url}" if digest.url and detail <= 1 else ""
    glosses = digest.glosses((3, 2, 1, 0, 0)[detail])
    words_line = ("｜今日词：" + "；".join(f"{word}（{gloss}）" for word, gloss in glosses)) if glosses else ""
    tail = (words_line + link).strip("｜")

    packer = _Packer(platform, reserve=tail)
    packer.line(f"{tags} 今日外刊精读：{digest.title}", required=True)
    if tail:
        packer.line("")
        packer.release()
        packer.line(tail)
    return packer.text(), packer.satisfied


def _build_x(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """X / Twitter：英文一行新闻 + 关键事实 + 今日词单 + 1-2 个 hashtag，≤280 字符。"""
    tags = _tag_line(digest, platform, 2)
    glosses = digest.glosses(3)
    words_line = ("Today's words: " + ", ".join(word for word, _ in glosses)) if glosses else ""
    url_line = digest.url if detail == 0 else ""
    reserve = " ".join(part for part in (tags, words_line, url_line) if part)
    packer = _Packer(platform, reserve=reserve)
    packer.line(f"{platform.emoji} {digest.title}", required=True)
    packer.line("")
    fact = " ".join(_en_sentences(digest.body)[: 2 if detail <= 2 else 1])
    if fact:
        packer.line(_trim_words(fact, (40, 30, 20, 14, 14)[detail]))
        packer.line("")
    packer.release()
    if words_line:
        packer.line(words_line)
    if url_line:
        packer.line(url_line)
    if tags:
        packer.line(tags, required=True)
    return packer.text(), packer.satisfied


def _build_linkedin(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """LinkedIn：英文专业长帖（300-600 词）：hook → what happened → why it matters →
    vocabulary spotlight（释义+例句）→ structure → discussion → CTA → hashtags。"""
    vocab_limit = (8, 6, 5, 4, 3, 2, 2)[detail]
    with_examples = detail <= 1
    with_grammar = detail <= 3
    with_question = detail <= 4
    with_essay = detail <= 0
    body_words = (200, 200, 200, 150, 120, 80, 40)[detail]

    words = digest.words(vocab_limit)
    glosses = digest.glosses(vocab_limit)
    number = digest.number_en()
    topic_word = (list(digest.hits[:1]) or [item.get("word", "") for item in words[:1]] or ["news"])[0]
    if number:
        hook = f"How do you say “{number}” in English?"
    else:
        hook = f"Today's {topic_word} headline, graded for busy professionals."

    blocks: list[str] = [f"{platform.emoji} {hook}"]
    body = _clean(digest.english_body(max_words=body_words)) or _clean(digest.title)
    blocks += ["What happened", body]

    names = list(digest.hits[:2]) or [
        _as_text(item.get("word")).lower() for item in words[:2] if _as_text(item.get("word"))
    ]
    spotlight_names = _join_and(names) or "headline, source and context"
    blocks += [
        "Why this matters if you work in English",
        (
            f"Stories like this one are dense with words such as {spotlight_names}. "
            "Reading one graded news piece a day moves vocabulary from passive recognition to the language "
            "you actually use in meetings, memos and email threads — without memorising word lists."
        ),
    ]
    if words:
        lines = ["Vocabulary to know"]
        for item, (word, _gloss) in zip(words, glosses):
            definition = _trim_words(_as_text(item.get("en_def")), 16).rstrip(". ") or "see the article for usage"
            entry = f"{word} ({_as_text(item.get('pos')).rstrip('.') or 'n.'}) — {definition}"
            example = _trim_words(_as_text(item.get("example")), 14) if with_examples else ""
            entry += f". In context: {example}" if example else "."
            lines.append("• " + _clean(entry))
        blocks += ["\n".join(lines)]
    if with_grammar and digest.grammar:
        lines = ["A structure worth stealing"]
        for item in digest.grammar[: 2 if detail == 0 else 1]:
            lines.append(f"• {_clean(item.get('sentence'))}")
        blocks += ["\n".join(lines)]
    if with_essay and digest.essay:
        blocks += ["Model paragraph (graded essay)", _trim_words(digest.essay, 60)]
    if with_question and digest.questions:
        blocks += ["Over to you", _clean(digest.questions[0])]

    follow = (
        "I publish a CEFR-graded news lesson every day — A1 to C2 texts, bilingual notes, audio and a short quiz. "
        "Follow EnglishFoundry and save one headline a day."
    )
    closing = follow + (f" Today's piece: {digest.url}" if digest.url and detail <= 2 else "")
    blocks.append(closing)
    tags = _tag_line(digest, platform, 3)
    if tags:
        blocks.append(tags)
    return "\n\n".join(block for block in (b.strip() for b in blocks) if block) + "\n", True


def _build_generic(platform: Platform, digest: _Digest, detail: int) -> tuple[str, bool]:
    """自定义平台兜底：标题 + 事件句 + 词表 + 标签，按语言选中/英文框架，长度按 unit 收缩。"""
    pick = (4, 3, 2, 1, 1)[detail]
    words = digest.words(pick)
    glosses = digest.glosses(pick)
    tags = _tag_line(digest, platform, 3)
    if platform.is_chinese:
        packer = _Packer(platform, reserve=tags)
        packer.line(_headline_hook(digest, platform, len(words)), required=True)
        packer.line("")
        packer.line(f"📌 {digest.title}", required=True)
        event = digest.english_body(max_words=(30, 24, 16, 12, 12)[detail])
        if event:
            packer.line(f"🗞 {event}")
        for word, gloss in glosses:
            packer.line(f"🔸 {word}｜{gloss}")
        packer.line(digest.cta or "完整版在主页，评论区回「学习」。")
        packer.release()
        if tags:
            packer.line(tags, required=True)
        return packer.text(), packer.satisfied
    blocks = [f"{platform.emoji} {_clean(digest.title)}".strip()]
    body = _clean(digest.english_body(max_words=(120, 90, 60, 40, 30)[detail]))
    if body:
        blocks.append(body)
    if words:
        blocks.append("Vocabulary: " + ", ".join(_as_text(item.get("word")) for item in words))
    blocks.append("Follow for a daily graded news lesson, and save this post." + (f" Link: {digest.url}" if digest.url else ""))
    if tags:
        blocks.append(tags)
    return "\n\n".join(block for block in blocks if block) + "\n", True


#: 平台 key → builder；未知 key 走 _build_generic，配置里新增平台也能直接渲染
_BUILDERS: dict[str, Callable[[Platform, _Digest, int], tuple[str, bool]]] = {
    "xiaohongshu": _build_xiaohongshu,
    "wechat_mp": _build_wechat_mp,
    "shortvideo": _build_shortvideo,
    "weibo": _build_weibo,
    "x": _build_x,
    "linkedin": _build_linkedin,
}

#: 各平台的降级档数（档位越高内容越精简；短平台还带 packer 保底，不会超上限）
_MAX_DETAIL: dict[str, int] = {"linkedin": 6, "wechat_mp": 4, "xiaohongshu": 4, "shortvideo": 4, "weibo": 4, "x": 4}
_GENERIC_MAX_DETAIL = 4


def _hard_clip(text: str, platform: Platform) -> str:
    """最后兜底：按行/词边界截到上限内（builder 正常都不会走到这里）。"""
    if platform.count(text) <= platform.char_limit:
        return text
    if platform.unit == "words":
        return _cut_words(text, platform.char_limit, marker="…")
    budget = platform.char_limit - 1
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) <= budget:
            kept.append(line)
            used += len(line)
        else:
            tail = _trim_chars(line, budget - used)
            if tail:
                kept.append(tail)
            break
    return "\n".join(kept).rstrip() + "…"


def _render_piece(platform: Platform, digest: _Digest) -> str:
    """从最丰富档位起逐档降级：优先“装得下且没丢内容”的档，其次首个装得下的档，最后硬截。"""
    builder = _BUILDERS.get(platform.key, _build_generic)
    max_detail = _MAX_DETAIL.get(platform.key, _GENERIC_MAX_DETAIL)
    first_fit = ""
    last = ""
    for detail in range(max_detail + 1):
        text, satisfied = builder(platform, digest, detail)
        last = text
        if text.strip() and platform.count(text) <= platform.char_limit:
            if first_fit == "":
                first_fit = text
            if satisfied:
                return text
    chosen = first_fit or last
    if platform.count(chosen) > platform.char_limit:
        chosen = _hard_clip(chosen, platform)
    return chosen if chosen.endswith("\n") else chosen + "\n"


# ---------------------------------------------------------------- 对外渲染
def render_for_platform(
    role_markdown: str,
    meta: Mapping[str, Any] | None,
    platform_key: str,
    platforms: Mapping[str, Platform] | None = None,
    settings: Settings | None = None,
) -> str:
    """把角色 Markdown 适配成某一条平台文案。

    Args:
        role_markdown: :func:`foundry.roles.render_for_role` 的产出（或同骨架的 Markdown）。
        meta: 补充信息映射（``title`` / ``url`` / ``source`` / ``date`` / ``level`` / ``story_id`` /
            ``role`` / ``tags``），可空；给了就覆盖 Markdown 里解析到的同名内容。
        platform_key: 平台 key，如 ``xiaohongshu``、``x``（大小写/连字符不敏感）。
        platforms: 预先加载好的平台表（批量渲染时传入，避免每个平台重读一次 yaml）。
        settings: 只在没传 ``platforms`` 且需要定位 ``config/platforms.yaml`` 时用。
    Returns:
        平台文案：保证 :meth:`Platform.fits` 为真；中文平台 = 中文框架 + 英文学习内容，
        英文平台全英文。
    Raises:
        PlatformsError: 平台不存在或配置非法。
    """
    table = platforms if platforms is not None else load_platforms(settings=settings)
    platform = platform_for(platform_key, table)
    digest = _apply_meta(_digest_of(_as_text(role_markdown)), meta)
    if not digest.title:  # 连标题都没有时给个兜底，保证各平台仍产出非空文案
        digest.title = "EnglishFoundry Daily English"
    text = _render_piece(platform, digest)
    if not platform.fits(text):
        log.warning("平台 %s 的产出仍超限（%s/%s），已硬截", platform.key, platform.count(text), platform.char_limit)
        text = _hard_clip(text, platform)
    return text


def render_all_platforms(
    role_markdown: str,
    meta: Mapping[str, Any] | None = None,
    platform_keys: Sequence[str] | None = None,
    platforms: Mapping[str, Platform] | None = None,
) -> dict[str, str]:
    """一次渲染多个平台（默认全部），返回 ``{platform_key: 文案}``。"""
    table = platforms if platforms is not None else load_platforms()
    keys = list(platform_keys) if platform_keys else list(table)
    return {key: render_for_platform(role_markdown, meta, key, table) for key in keys}


# ---------------------------------------------------------------- 命令行
def _main(argv: Sequence[str] | None = None) -> int:
    """看一眼平台表或渲染结果：``python -m foundry.platforms --list`` / ``--pack ... --role business``"""
    parser = argparse.ArgumentParser(description="把角色 Markdown 适配成各平台文案（config/platforms.yaml）")
    parser.add_argument("--list", action="store_true", help="打印全部平台配置摘要")
    parser.add_argument("--pack", metavar="PATH", help="母版 JSON（走 roles.render_for_role 现渲染角色 Markdown）")
    parser.add_argument("--role", default="business", help="配合 --pack 的角色 key（默认 business）")
    parser.add_argument("--markdown", metavar="PATH", help="直接读一份角色 Markdown 文件")
    parser.add_argument("--meta", metavar="PATH", help="补充信息 JSON（title/url/source/date/...）")
    parser.add_argument("--platform", default="all", help="平台 key，逗号分隔多个（默认 all）")
    parser.add_argument("--platforms-file", metavar="PATH", help="平台配置（默认 config/platforms.yaml）")
    parser.add_argument("--out", metavar="DIR", help="把渲染结果写成 <平台>.md 到该目录")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印降级过程等 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        platforms = load_platforms(args.platforms_file)
    except PlatformsError as error:
        print(f"{error}")
        return 1

    if args.list:
        print(f"共 {len(platforms)} 个平台（{PLATFORMS_FILE_NAME}）")
        for platform in platforms.values():
            print(f"  {platform.summary()}")
            print(f"      format: {platform.format}")
            print(f"      title:  {platform.title_rule}")
            print(f"      notes:  {platform.notes}")
        return 0

    role_markdown = ""
    meta: Mapping[str, Any] = {}
    if args.meta:
        meta_path = Path(args.meta).expanduser()
        if not meta_path.is_file():
            print(f"找不到 meta JSON：{meta_path}")
            return 1
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if args.pack:
        from foundry.models import ContentPack  # 只在需要母版渲染时才拉模型
        from foundry.roles import render_for_role

        source = Path(args.pack).expanduser()
        if not source.is_file():
            print(f"找不到母版文件：{source}")
            return 1
        pack = ContentPack.load(source)
        try:
            role_markdown = render_for_role(pack, args.role)
        except Exception as error:  # RolesError 等，CLI 只报可读信息
            print(f"角色渲染失败：{error}")
            return 1
    elif args.markdown:
        source = Path(args.markdown).expanduser()
        if not source.is_file():
            print(f"找不到角色 Markdown：{source}")
            return 1
        role_markdown = source.read_text(encoding="utf-8")
    else:
        print("需要 --pack 指向母版 JSON，或 --markdown 指向角色 Markdown（或只用 --list 看平台表）")
        return 1

    keys = (
        list(platforms)
        if args.platform.strip().lower() in {"all", ""}
        else [key for key in re.split(r"[,\s]+", args.platform) if key]
    )
    output_dir = Path(args.out).expanduser() if args.out else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for key in keys:
        try:
            text = render_for_platform(role_markdown, meta, key, platforms)
        except PlatformsError as error:
            failures += 1
            print(f"{key}: 失败 -> {error}")
            continue
        platform = platform_for(key, platforms)
        unit_name = "词" if platform.unit == "words" else "字"
        print(f"[{key}] {platform.count(text)} {unit_name} / 上限 {platform.char_limit}，{len(text)} 字符")
        if output_dir is not None:
            target = output_dir / f"{key}.md"
            target.write_text(text, encoding="utf-8")
            print(f"    已写 {target}")
        else:
            print(text)
    return 0 if failures == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

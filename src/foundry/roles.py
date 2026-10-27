"""``ContentPack`` → 12 个角色的差异化 Markdown（配置在 ``config/roles.yaml``）。

分工与约定：
    * 输入 master 产出的 :class:`foundry.models.ContentPack` + 角色 key，输出一段 Markdown；
      本模块不联网、不调 LLM、不写死角色文案，纯离线可跑；写盘留给 ``platforms`` / ``pipeline``。
    * 角色只来自 ``config/roles.yaml``（:func:`load_roles`）：``key`` / ``name`` / ``levels`` /
      ``modules`` / ``tone`` / ``word_limit`` / ``extra`` 七个字段。``modules`` 复用 master 的
      别名表（``vocab`` → ``vocabulary``、``reading`` → ``levels``），未知键直接报错，
      渲染时再与母版里**实际有内容**的键取交集，缺哪个键就少哪个板块，不会崩。
    * 选材：``levels`` 是偏好顺序，取母版里存在的第一个非空档；母版里一档都没有时按 CEFR
      距离就近回退（考研角色拿到只有 A1 的母版也能出稿，并在开头标明“已回退”）。
    * 词数：``word_limit`` 是**整篇** Markdown 的硬上限，英文按词、中文按字、数字按串计
      （:func:`count_words`，一套正则同时服务计数与截断）。超预算时按 :meth:`Role.trim_order`
      （默认 :data:`DEFAULT_TRIM_ORDER`：写作 → 听力 → 背景 → 口语 → 双语 → 语法 → 练习 →
      词汇 → 正文）把板块一档一档降级（丢例句 → 丢英文释义 → 减条数 → 题干压成一行 →
      整段去掉），正文永远最后动刀，保证骨架稳定；压完再做一次“回填”，把最后砍掉的
      板块逐个还原，避免一步砍过头、产出比预算小一大截。
    * 骨架固定：``# 标题`` → 角色说明块（tone / focus / 题材词命中）→ 正文 → 各学习板块 →
      练习 → ``<details>`` 答案折叠区 → 结尾任务。下游 platforms 只做排版，不再改内容。
    * 角色“侧重”全部写在 ``extra``：正文词数、各段条数、题材关键词（命中的词在词汇表里
      ⭐ 标记，:func:`topic_match` 给 0-1 契合度供 pipeline 排序筛选）、结尾任务，
      以及 ``tts_voice`` / ``tts_rate`` 提示（本模块只透传信息，配音由 tts 模块取用）。

用法::

    from foundry.models import ContentPack
    from foundry.roles import load_roles, render_for_role

    pack = ContentPack.load("output/packs/demo-eu-data-fine.json")
    markdown = render_for_role(pack, "kids")

手工看一眼::

    python -m foundry.roles --list
    python -m foundry.roles --pack tests/fixtures/sample_pack.json --role developer
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from foundry.config import Settings, find_project_root
from foundry.master import CEFR_LEVELS, MasterError, resolve_modules
from foundry.models import ContentPack

__all__ = [
    "DEFAULT_TRIM_ORDER",
    "EXPECTED_ROLES",
    "MAX_DETAIL",
    "ROLES_FILE_NAME",
    "SECTION_ORDER",
    "Role",
    "RolesError",
    "count_words",
    "load_roles",
    "render_all",
    "render_for_role",
    "role_for",
    "roles_path",
    "topic_match",
]

log = logging.getLogger("foundry.roles")

#: 角色配置文件名（放在项目根 config/ 下）
ROLES_FILE_NAME = "roles.yaml"
#: 内置的 12 个角色 key（顺序 = 站点导航顺序；缺失只告警，不报错）
EXPECTED_ROLES: tuple[str, ...] = (
    "k12",
    "postgrad",
    "cet",
    "ielts_toefl",
    "business",
    "trade",
    "crossborder",
    "developer",
    "academic",
    "creator",
    "immigration",
    "kids",
)
DEFAULT_WORD_LIMIT = 800
#: 正文最多占 word_limit 的这份比例，剩下的留给词汇/练习等板块（防止一段吃掉全篇预算）
BODY_RATIO = 0.5
MIN_BODY_WORDS = 25  # 正文被预算压缩时的地板，避免缩成一两句没法读
CHOICE_LETTERS = "ABCDEFGHIJ"

# ---- 词计数：CJK 单字、拉丁单词（带连字符/撇号）、数字串各算一个 ----
_WORD_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
    r"|[A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f'\-]*"
    r"|\d+(?:[.,]\d+)*"
)
_SENTENCE_RE = re.compile(r"[^.!?。！？\n]+[.!?。！？]*")

#: 母版键 → 产出里的板块顺序（也是文档顺序）
SECTION_ORDER: tuple[str, ...] = (
    "levels",
    "background_zh",
    "bilingual",
    "vocabulary",
    "grammar",
    "listening_script",
    "speaking",
    "writing",
    "quiz",
)
#: 默认压缩顺序：越靠前越先让步（拓展类先走，词汇/练习/正文最后动刀）
DEFAULT_TRIM_ORDER: tuple[str, ...] = (
    "writing",
    "listening_script",
    "background_zh",
    "speaking",
    "bilingual",
    "grammar",
    "quiz",
    "vocabulary",
    "levels",
)
#: 每个板块最多能降到第几档（见各渲染函数里的 detail 分支）
MAX_DETAIL: dict[str, int] = {
    "levels": 4,
    "background_zh": 2,
    "bilingual": 2,
    "vocabulary": 3,
    "grammar": 3,
    "listening_script": 2,
    "speaking": 3,
    "writing": 3,
    "quiz": 4,
}
#: 正文按 detail 档位缩水的比例（1.0 → 30%）
_BODY_FACTORS: tuple[float, ...] = (1.0, 0.8, 0.6, 0.45, 0.3)


class RolesError(RuntimeError):
    """角色配置缺失或非法（字段写错、模块名不认识、levels 不是 CEFR 档位）。"""


# ---------------------------------------------------------------- 小工具
def _as_text(value: Any) -> str:
    """标量取字符串；映射/列表当空（母版偶尔会把字段写成数组，这里不做猜测）。"""
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return ""
    return str(value).strip()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _records(value: Any) -> list[dict[str, Any]]:
    """取列表里的映射项（非映射项丢掉）。"""
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [text for text in (_as_text(item) for item in value) if text]


def _norm_role_key(value: Any) -> str:
    """角色 key 归一：小写、空格/连字符都当下划线（``IELTS-Toefl`` -> ``ielts_toefl``）。"""
    return re.sub(r"[\s\-]+", "_", _as_text(value).lower().strip("_"))


def _norm_level(value: Any) -> str:
    """难度档归一成大写 CEFR 键（``b1``/``B1+``/``Level B1`` -> ``B1``）；不认识返回空串。"""
    match = re.search(r"([A-C])\s*([1-6])", _as_text(value).upper())
    if not match:
        return ""
    level = f"{match.group(1)}{match.group(2)}"
    return level if level in CEFR_LEVELS else ""


def count_words(text: str) -> int:
    """词数：中文按字、英文按词、数字串各算 1（对中英混排的产出也成立）。"""
    if not text:
        return 0
    return len(_WORD_RE.findall(text))


def _cut_words(text: str, limit: int, marker: str = " …") -> str:
    """硬截断到 ``limit`` 个词（按词边界切，不砍半个词）。"""
    if limit <= 0:
        return ""
    spans = [match.span() for match in _WORD_RE.finditer(text)]
    if len(spans) <= limit:
        return text
    end = spans[limit - 1][1]
    return text[:end].rstrip(" ，,、;；:：") + marker


def _trim_words(text: str, limit: int) -> str:
    """按句子截断：整句累加到 limit 词为止；首句过长时退回硬截断，保证不超。"""
    text = _as_text(text)
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
    if count_words(result) > limit:  # 首句本身就超预算
        result = _cut_words(result, limit, marker="")
    return result.rstrip(" .,;:!?") + " …"


def _clip_words(value: Any, limit: int) -> str:
    """按词截断（词汇表的英文释义用，别让一行释义拖垮整张表）。"""
    return _cut_words(_as_text(value), limit)


def _clip_chars(text: str, limit: int) -> str:
    """按字符截断（给中文解析、背景这类短文本用）。"""
    text = _as_text(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip(" ，,。;；:：") + "…"


def _md_cell(value: Any) -> str:
    """表格单元格：压平空白并转义竖线，别让一条例句把表撑坏。"""
    return re.sub(r"\s+", " ", _as_text(value)).replace("|", "\\|")


# ---------------------------------------------------------------- 角色
@dataclass(frozen=True)
class Role:
    """一个角色：难度偏好 + 取用的母版键 + 语气 + 全篇词数上限 + 专属旋钮。"""

    key: str
    name: str
    levels: tuple[str, ...] = ("B1",)
    modules: tuple[str, ...] = ()
    tone: str = ""
    word_limit: int = DEFAULT_WORD_LIMIT
    extra: Mapping[str, Any] = field(default_factory=dict)

    # ---- extra 读取 ----
    def count(self, name: str, default: int) -> int:
        """取一个正整数旋钮（写成 0/负数/非数字都退回默认值）。"""
        value = _as_int(self.extra.get(name), 0)
        return value if value > 0 else default

    @property
    def keywords(self) -> tuple[str, ...]:
        return tuple(_as_text(item).lower() for item in _strings(self.extra.get("keywords")) if _as_text(item))

    @property
    def emoji(self) -> str:
        return _as_text(self.extra.get("emoji"))

    @property
    def focus(self) -> str:
        return _as_text(self.extra.get("focus"))

    @property
    def cta(self) -> str:
        return _as_text(self.extra.get("cta"))

    @property
    def body_words(self) -> int:
        """正文目标词数：``extra.body_words``，但不超过全篇预算的一半。"""
        ceiling = max(MIN_BODY_WORDS, int(self.word_limit * BODY_RATIO))
        wanted = self.count("body_words", ceiling)
        return min(wanted, ceiling)

    def trim_order(self, available: Sequence[str]) -> tuple[str, ...]:
        """压缩顺序：``extra.trim_order`` 声明的在前（先牺牲），其余按 :data:`DEFAULT_TRIM_ORDER`。

        角色“宁可砍哪一段”是它的侧重之一，所以交给配置说；没声明或用不上的键忽略。
        """
        declared = [name for name in _strings(self.extra.get("trim_order")) if name in available]
        rest = [name for name in DEFAULT_TRIM_ORDER if name in available and name not in declared]
        return tuple(declared + rest)

    def tts_hint(self) -> dict[str, str]:
        """配音提示（本模块不配音，只把 roles.yaml 里的设置透传给 tts 模块）。"""
        hint = {}
        for name in ("tts_voice", "tts_rate"):
            value = _as_text(self.extra.get(name))
            if value:
                hint[name] = value
        return hint

    def summary(self) -> str:
        """一行摘要，给 ``--list`` 和日志用。"""
        return (
            f"{self.key:<12} {self.name:<12} levels={'>/'.join(self.levels):<9} "
            f"limit={self.word_limit:<5} modules={', '.join(self.modules)}"
        )


def roles_path(path: str | Path | None = None, settings: Settings | None = None) -> Path:
    """定位角色配置：显式 path > ``settings.root`` > 自动发现的项目根，相对路径按项目根解析。"""
    if path is not None:
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate
        root = settings.root if settings is not None else find_project_root()
        return (root / candidate).resolve()
    root = settings.root if settings is not None else find_project_root()
    return (root / "config" / ROLES_FILE_NAME).resolve()


def _coerce_entries(payload: Any, source: Path) -> list[Mapping[str, Any]]:
    """把 yaml 里的 roles 段取成条目列表（支持 ``roles: {key: {...}}`` 与 ``roles: [...]``）。"""
    if isinstance(payload, Mapping) and isinstance(payload.get("roles"), (Mapping, list, tuple)):
        payload = payload["roles"]
    if isinstance(payload, Mapping):
        entries = []
        for key, value in payload.items():
            item = dict(value) if isinstance(value, Mapping) else {"name": value}
            item.setdefault("key", key)
            entries.append(item)
        return entries
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    raise RolesError(f"{source} 的 roles: 必须是映射或列表，实际为 {type(payload).__name__}")


def _build_role(entry: Mapping[str, Any], source: Path) -> Role:
    """校验并构造一个 Role：字段名宽容（key/name 缺省可推），值不合法就报错。"""
    key = _norm_role_key(entry.get("key") or entry.get("id") or entry.get("name"))
    if not key:
        raise RolesError(f"{source}：有条目缺 key/id/name，无法确定角色标识：{entry!r}")
    name = _as_text(entry.get("name")) or key

    raw_levels = entry.get("levels")
    levels: list[str] = []
    for item in _strings(raw_levels):
        level = _norm_level(item)
        if not level:
            raise RolesError(f"{source}：角色 {key} 的 levels 含非 CEFR 档位 {item!r}（可选 A1-C2）")
        if level not in levels:
            levels.append(level)
    if not levels:
        raise RolesError(f"{source}：角色 {key} 至少要有一个 levels（如 [B1, B2]）")

    try:
        modules = resolve_modules(entry.get("modules"))
    except MasterError as error:
        raise RolesError(f"{source}：角色 {key} 的 modules 有问题 -> {error}") from error

    limit = _as_int(entry.get("word_limit"), DEFAULT_WORD_LIMIT)
    if limit <= 0:
        raise RolesError(f"{source}：角色 {key} 的 word_limit 必须是正整数，实际为 {entry.get('word_limit')!r}")

    raw_extra = entry.get("extra")
    if raw_extra is None:
        extra: Mapping[str, Any] = {}
    elif isinstance(raw_extra, Mapping):
        extra = dict(raw_extra)
    else:
        raise RolesError(f"{source}：角色 {key} 的 extra 必须是键值映射")

    return Role(
        key=key,
        name=name,
        levels=tuple(levels),
        modules=tuple(item for item in SECTION_ORDER if item in modules),
        tone=_as_text(entry.get("tone")),
        word_limit=limit,
        extra=extra,
    )


def load_roles(path: str | Path | None = None, settings: Settings | None = None) -> dict[str, Role]:
    """读取角色配置，返回 ``{key: Role}``（顺序 = 文件里的顺序）。

    Args:
        path: 显式指定角色 yaml；默认 ``<项目根>/config/roles.yaml``。
        settings: 已加载的配置（用来取项目根），不传则自动定位项目根。
    Raises:
        RolesError: 文件不存在、顶层不是映射、字段非法。
    """
    source = roles_path(path, settings)
    if not source.is_file():
        raise RolesError(
            f"找不到角色配置：{source}。请创建该文件并写入 roles: 段（12 个角色见 prompts/build/04_roles.md）。"
        )
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:  # 缩进/引号错误要说清是哪个文件
        raise RolesError(f"{source} 不是合法 YAML：{error}") from error
    if payload is None:
        raise RolesError(f"{source} 是空的，至少需要 roles: 段")

    roles: dict[str, Role] = {}
    for entry in _coerce_entries(payload, source):
        role = _build_role(entry, source)
        if role.key in roles:
            raise RolesError(f"{source}：角色 key 重复：{role.key}")
        roles[role.key] = role
    if not roles:
        raise RolesError(f"{source} 里一个角色都没有（roles: 段为空）")

    missing = [key for key in EXPECTED_ROLES if key not in roles]
    if missing:
        log.warning("%s 缺少约定角色：%s", source.name, ", ".join(missing))
    log.debug("已加载 %s 个角色：%s", len(roles), ", ".join(roles))
    return roles


def role_for(role_key: str, roles: Mapping[str, Role] | None = None) -> Role:
    """按 key 取角色（大小写/连字符不敏感）；找不到就报错并列出可用角色。"""
    table = roles if roles is not None else load_roles()
    normalized = _norm_role_key(role_key)
    for key, role in table.items():
        if _norm_role_key(key) == normalized:
            return role
    raise RolesError(f"没有这个角色：{role_key!r}（可用：{', '.join(table)}）")


# ---------------------------------------------------------------- 母版快照
@dataclass(frozen=True)
class _Snapshot:
    """母版内容的只读快照：形状统一，缺键就是空值，渲染函数不用再防 None。"""

    story_id: str = ""
    path: str = ""
    levels: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    bilingual: tuple[Mapping[str, Any], ...] = ()
    vocabulary: tuple[Mapping[str, Any], ...] = ()
    grammar: tuple[Mapping[str, Any], ...] = ()
    background_zh: str = ""
    listening_script: str = ""
    speaking: Mapping[str, Any] = field(default_factory=dict)
    writing: Mapping[str, Any] = field(default_factory=dict)
    quiz: tuple[Mapping[str, Any], ...] = ()

    @property
    def text_index(self) -> str:
        """把所有可读文本拼起来，用来做题材关键词匹配。"""
        parts = [self.background_zh, self.listening_script]
        for title, body in self.levels.values():
            parts.extend((title, body))
        for item in self.vocabulary:
            parts.extend((_as_text(item.get("word")), _as_text(item.get("example"))))
        for item in self.bilingual:
            parts.append(_as_text(item.get("en")))
        for item in self.grammar:
            parts.append(_as_text(item.get("sentence")))
        return " ".join(part for part in parts if part)

    def content_of(self, module: str) -> bool:
        """母版里这个键有没有可用内容（没有就跳过板块，不硬凑）。"""
        if module == "levels":
            return any(body for _, body in self.levels.values())
        value = getattr(self, module, None)
        if isinstance(value, str):
            return bool(value.strip())
        return bool(value)


def _snapshot(pack: ContentPack | Mapping[str, Any]) -> _Snapshot:
    """把 ContentPack（或其 data 映射）读成 :class:`_Snapshot`。"""
    if isinstance(pack, ContentPack):
        data, story_id, path = pack.data, pack.story_id, pack.path
    elif isinstance(pack, Mapping):
        data = dict(pack)
        story_id = _as_text(data.get("story_id"))
        path = data.get("path")
    else:
        raise RolesError(f"render_for_role 需要 ContentPack 或映射，实际为 {type(pack).__name__}")

    levels: dict[str, tuple[str, str]] = {}
    raw_levels = data.get("levels")
    if isinstance(raw_levels, Mapping):
        for raw_key, entry in raw_levels.items():
            level = _norm_level(raw_key)
            if not level or not isinstance(entry, Mapping):
                continue
            levels[level] = (_as_text(entry.get("title")), _as_text(entry.get("body")))
    elif isinstance(raw_levels, (list, tuple)):  # 容忍 [{level: B1, ...}] 这种写法
        for entry in raw_levels:
            if isinstance(entry, Mapping):
                level = _norm_level(entry.get("level") or entry.get("cefr"))
                if level:
                    levels[level] = (_as_text(entry.get("title")), _as_text(entry.get("body")))

    return _Snapshot(
        story_id=story_id or (Path(path).stem if path else ""),
        path=_as_text(path),
        levels=levels,
        bilingual=tuple(_records(data.get("bilingual"))),
        vocabulary=tuple(_records(data.get("vocabulary"))),
        grammar=tuple(_records(data.get("grammar"))),
        background_zh=_as_text(data.get("background_zh")),
        listening_script=_as_text(data.get("listening_script")),
        speaking=dict(data.get("speaking")) if isinstance(data.get("speaking"), Mapping) else {},
        writing=dict(data.get("writing")) if isinstance(data.get("writing"), Mapping) else {},
        quiz=tuple(_records(data.get("quiz"))),
    )


def _pick_level(snap: _Snapshot, role: Role) -> tuple[str, str, str, bool]:
    """按角色偏好选难度档，返回 (level, title, body, 是否回退)。

    先在 ``role.levels`` 顺序里找“正文非空”的那档；一档都没有就按 CEFR 距离就近选，
    距离相同取更简单的档（宁可让用户读得动，也不硬上超纲内容）。
    """
    for level in role.levels:
        title, body = snap.levels.get(level, ("", ""))
        if body:
            return level, title, body, False
    available = [level for level, (_, body) in snap.levels.items() if body]
    if not available:
        return "", "", "", False
    wanted = [CEFR_LEVELS.index(level) for level in role.levels if level in CEFR_LEVELS]
    if not wanted:  # 角色偏好非法（理论上进不来），退回最简单的可用档
        level = available[0]
        return level, *snap.levels[level], True

    def distance(level: str) -> tuple[int, int]:
        index = CEFR_LEVELS.index(level)
        return (min(abs(index - item) for item in wanted), -index)

    level = min(available, key=distance)
    title, body = snap.levels[level]
    log.debug("角色 %s 偏好 %s，母版没有，回退到 %s", role.key, "/".join(role.levels), level)
    return level, title, body, True


# ---------------------------------------------------------------- 渲染上下文
@dataclass
class _Ctx:
    role: Role
    snap: _Snapshot
    level: str
    title: str
    body: str
    fallback: bool
    modules: tuple[str, ...]
    hits: tuple[str, ...] = ()
    body_cap: int = 0
    trim: tuple[str, ...] = ()

    @property
    def keyword_set(self) -> set[str]:
        return set(self.hits)


def _keyword_hits(role: Role, snap: _Snapshot, body: str) -> tuple[str, ...]:
    """角色关键词在本篇的命中（前缀匹配，user 命中 users）；按母版里出现的先后排序。"""
    if not role.keywords:
        return ()
    haystack = f"{body} {snap.text_index}".lower()
    hits: list[str] = []
    for keyword in role.keywords:
        if re.search(rf"\b{re.escape(keyword)}\w*", haystack) and keyword not in hits:
            hits.append(keyword)
    return tuple(hits)


def topic_match(pack: ContentPack | Mapping[str, Any], role: Role | str, roles: Mapping[str, Role] | None = None) -> float:
    """本篇母版对某角色的题材契合度（0-1）：命中的角色关键词 / 该角色关键词总数。

    给 pipeline 用来决定“这篇要不要给这个角色出稿”，没配 keywords 的角色返回 0.0。
    """
    active = role if isinstance(role, Role) else role_for(role, roles)
    if not active.keywords:
        return 0.0
    snap = _snapshot(pack)
    body = ""
    for level in active.levels:
        if snap.levels.get(level, ("", ""))[1]:
            body = snap.levels[level][1]
            break
    hits = _keyword_hits(active, snap, body)
    return round(len(hits) / len(active.keywords), 4)


# ---------------------------------------------------------------- 各板块渲染
def _section_levels(ctx: _Ctx, detail: int) -> str:
    """正文（选定难度档改写）。detail 越高，正文按 _BODY_FACTORS 逐档缩短。"""
    if not ctx.body:
        return ""
    factor = _BODY_FACTORS[min(detail, len(_BODY_FACTORS) - 1)]
    cap = int(ctx.body_cap * factor)
    if detail:  # 被预算压缩时才保住“读得完的一段”，否则照配置给
        cap = max(MIN_BODY_WORDS, cap)
    body = _trim_words(ctx.body, cap)
    return "\n".join(
        [
            f"## 📖 正文 · CEFR {ctx.level}",
            "",
            f"**{ctx.title}**" if ctx.title else "",
            "",
            body,
        ]
    ).strip()


def _section_background(ctx: _Ctx, detail: int) -> str:
    """中文背景速览（超预算时先砍到 extra.background_chars，再整段去掉）。"""
    if not ctx.snap.background_zh:
        return ""
    limit = len(ctx.snap.background_zh) if detail == 0 else ctx.role.count("background_chars", 80)
    text = _clip_chars(ctx.snap.background_zh, limit)
    if not text:
        return ""
    return f"## 🧭 背景速览\n\n{text}"


def _section_bilingual(ctx: _Ctx, detail: int) -> str:
    """逐句中英对照（考研/精读类角色的主菜；预算紧就少给几句，最后整段去掉）。"""
    wanted = ctx.role.count("bilingual_count", 6)
    pairs = [item for item in ctx.snap.bilingual if _as_text(item.get("en"))][:wanted]
    if detail >= 2:
        return ""
    if detail == 1:
        pairs = pairs[: max(2, len(pairs) // 2)]
    if not pairs:
        return ""
    lines = ["## 🔎 逐句精读", ""]
    for item in pairs:
        lines.append(f"- {_as_text(item.get('en'))}")
        translation = _as_text(item.get("zh"))
        if translation:
            lines.append(f"  - 译：{translation}")
    return "\n".join(lines)


def _section_vocabulary(ctx: _Ctx, detail: int) -> str:
    """核心词汇表：detail 0 全字段 → 1 去例句 → 2 去英文释义 → 3 词量减半（角色多词汇的坚持到此为止）。"""
    wanted = ctx.role.count("vocab_count", len(ctx.snap.vocabulary) or 8)
    items = list(ctx.snap.vocabulary)[:wanted]
    if detail >= 3 and len(items) > 1:
        items = items[: max(1, -(-len(items) // 2))]
    if not items:
        return ""
    with_example = detail == 0
    with_definition = detail <= 1
    headers = ["#"]
    for label in ("单词", "词性", "中文"):
        headers.append(label)
    if with_definition:
        headers.append("英文释义")
    if with_example:
        headers.append("例句")
    example_cap = ctx.role.count("example_words", 14)
    lines = [
        f"## 📚 核心词汇（{len(items)} 个）",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join([" --- "] * len(headers)) + "|",
    ]
    for index, item in enumerate(items, start=1):
        word = _as_text(item.get("word"))
        starred = " ⭐" if _matches_any(word, ctx.keyword_set) else ""
        row = [str(index), f"**{word}**{starred}", _md_cell(item.get("pos")), _md_cell(item.get("zh"))]
        if with_definition:
            row.append(_md_cell(_clip_words(item.get("en_def"), 16)))
        if with_example:
            row.append(_md_cell(_trim_words(_as_text(item.get("example")), example_cap)))
        lines.append("| " + " | ".join(row) + " |")
    if ctx.hits:
        lines += ["", f"> ⭐ 命中的 **{ctx.role.name}** 方向关键词：{'、'.join(ctx.hits)}"]
    return "\n".join(lines)


def _matches_any(word: str, keywords: set[str]) -> bool:
    lowered = _as_text(word).lower()
    if not lowered or not keywords:
        return False
    return any(re.search(rf"\b{re.escape(keyword)}\w*", lowered) for keyword in keywords)


def _section_grammar(ctx: _Ctx, detail: int) -> str:
    """长难句 / 语法点：讲解文字按 240 → 120 → 40 字递减，最后只留一句，再不行整段去掉。"""
    items = [item for item in ctx.snap.grammar if _as_text(item.get("sentence"))]
    wanted = ctx.role.count("grammar_count", len(items) or 3)
    items = items[:wanted]
    if detail >= 3 or not items:
        return ""
    if detail == 2:
        items = items[:1]
    char_limit = (240, 120, 40)[min(detail, 2)]
    lines = ["## 🔧 长难句拆解", ""]
    for index, item in enumerate(items, start=1):
        point = _as_text(item.get("point"))
        lines.append(f"**{index}. {item.get('sentence')}**")
        lines.append("")
        if point:
            lines.append(f"- 语法点：{point}")
        explanation = _clip_chars(item.get("explanation_zh"), char_limit)
        if explanation:
            lines.append(f"- 讲解：{explanation}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _section_listening(ctx: _Ctx, detail: int) -> str:
    """听力稿：默认给满 extra.listening_words，预算紧先砍到 60 词，再整段去掉。"""
    text = ctx.snap.listening_script
    if not text:
        return ""
    if detail >= 2:
        return ""
    cap = ctx.role.count("listening_words", 120) if detail == 0 else 60
    return f"## 🎧 听力稿（{ctx.level} 口语版）\n\n{_trim_words(text, cap)}"


def _section_speaking(ctx: _Ctx, detail: int) -> str:
    """口语：讨论问题 + 角色扮演。角色扮演的 scenario/roles/task 按档递减，最后只留问题。"""
    if detail >= 3:
        return ""
    speaking = ctx.snap.speaking
    questions = _strings(speaking.get("questions"))[: ctx.role.count("speaking_questions", 3)]
    roleplay = speaking.get("roleplay") if isinstance(speaking.get("roleplay"), Mapping) else {}
    if not questions and not roleplay:
        return ""
    lines = ["## 🗣 开口说", ""]
    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question}")
    if questions:
        lines.append("")
    if detail <= 1 and roleplay:
        label = "角色扮演（只给场景）" if detail == 1 else "角色扮演"
        lines.append(f"**{label}**：{_as_text(roleplay.get('title'))}")
        lines.append("")
        lines.append(f"- 场景：{_as_text(roleplay.get('scenario'))}")
        if detail == 0:
            for speaker in _strings(roleplay.get("roles")):
                lines.append(f"- 角色：{speaker}")
            task = _as_text(roleplay.get("task"))
            if task:
                lines.append(f"- 任务：{task}")
    return "\n".join(lines).rstrip()


def _section_writing(ctx: _Ctx, detail: int) -> str:
    """写作：题目 + 范文。范文词数 120 → 60 → 只给题目 → 去掉。"""
    topic = _as_text(ctx.snap.writing.get("topic"))
    essay = _as_text(ctx.snap.writing.get("essay"))
    if not topic and not essay:
        return ""
    if detail >= 3:
        return ""
    lines = ["## ✍️ 写作练习", ""]
    if topic:
        lines.append(f"**题目**：{topic}")
        lines.append("")
    if essay and detail <= 1:
        cap = ctx.role.count("essay_words", 140) if detail == 0 else 60
        lines.append(_trim_words(essay, cap))
    return "\n".join(lines).rstrip()


def _choice_pairs(item: Mapping[str, Any]) -> list[tuple[str, str]]:
    """选项统一成 [(A, 文本)]：列表按下标编号，映射按其字母键编号。"""
    raw = item.get("choices")
    if isinstance(raw, Mapping):
        pairs = []
        for letter in CHOICE_LETTERS:
            for key, value in raw.items():
                if _as_text(key).strip().upper().startswith(letter):
                    pairs.append((letter, _as_text(value)))
        return pairs
    if isinstance(raw, (list, tuple)):
        return [(CHOICE_LETTERS[index], _as_text(value)) for index, value in enumerate(raw) if _as_text(value)]
    return []


def _answer_letter(item: Mapping[str, Any], pairs: Sequence[tuple[str, str]]) -> str:
    """答案取字母：接受 ``B`` / ``(B)`` / ``B. 选项`` / 选项原文。"""
    answer = _as_text(item.get("answer")).upper()
    if not answer:
        return ""
    match = re.search(rf"[A-{CHOICE_LETTERS[len(pairs) - 1] if pairs else 'J'}]", answer)
    if match:
        return match.group()
    normalized = re.sub(r"[^a-z0-9]+", "", answer.lower())
    for letter, text in pairs:
        if re.sub(r"[^a-z0-9]+", "", text.lower()) == normalized and normalized:
            return letter
    return ""


def _section_quiz(ctx: _Ctx, detail: int) -> str:
    """练习 + 答案折叠区。

    detail 0 全解析 → 1 解析截短 → 2 只给答案字母 → 3 题量砍到 2 → 4 一行题干（先牺牲选项）。
    折叠区跟着阶梯留到最后一档：宁可少给选项也不撕掉答案；只有预算小到走 :func:`_truncate`
    兜底时才可能整段消失，那种情况下也保证 ``<details>`` 成对。
    """
    wanted = ctx.role.count("quiz_count", len(ctx.snap.quiz) or 5)
    items = [item for item in ctx.snap.quiz if _as_text(item.get("q"))]
    if detail >= 3:
        wanted = min(wanted, 2)
    items = items[:wanted]
    if not items:
        return ""
    lines = [f"## 📝 小练习（{len(items)} 题）", ""]
    key: list[str] = []
    answers: list[tuple[int, str, str]] = []
    for index, item in enumerate(items, start=1):
        pairs = _choice_pairs(item)
        letter = _answer_letter(item, pairs)
        key.append(f"{index}.{letter or '?'}")
        answers.append((index, letter or "?", _as_text(item.get("explain"))))
        if detail >= 4:  # 预算最紧的一档：一行题干 + 答案速查，练习骨架仍在
            lines.append(f"{index}. {_trim_words(_as_text(item.get('q')), 16)}")
            continue
        lines.append(f"**{index}.** {_as_text(item.get('q'))}")
        lines.append("")
        for option, text in pairs:
            lines.append(f"- **{option}.** {text}")
        lines.append("")
    lines += ["", "<details>", "<summary>✅ 参考答案与解析（点击展开）</summary>", ""]
    if detail <= 1:
        limit = ctx.role.count("explain_chars", 80) if detail == 0 else 40
        for index, letter, explain in answers:
            lines.append(f"{index}. **{letter}**" + (f" — {_clip_chars(explain, limit)}" if explain else ""))
    else:
        lines.append("答案速查：" + "　".join(key))
    lines += ["", "</details>"]
    return "\n".join(lines)


_SECTION_RENDERERS: dict[str, Callable[[_Ctx, int], str]] = {
    "levels": _section_levels,
    "background_zh": _section_background,
    "bilingual": _section_bilingual,
    "vocabulary": _section_vocabulary,
    "grammar": _section_grammar,
    "listening_script": _section_listening,
    "speaking": _section_speaking,
    "writing": _section_writing,
    "quiz": _section_quiz,
}


# ---------------------------------------------------------------- 组装
def _render_header(ctx: _Ctx) -> str:
    role = ctx.role
    title = ctx.title or f"{role.name} · {ctx.snap.story_id or '母版'}"
    heading = f"{role.emoji + ' ' if role.emoji else ''}{title}"
    lines = [f"# {heading}", "", f"> 角色：**{role.name}**（`{role.key}`）"]
    if ctx.level:
        wanted = "/".join(role.levels)
        level_note = f"（母版无 {wanted}，已就近回退）" if ctx.fallback else ""
        lines.append(f"> 难度：{ctx.level}{level_note} ｜ 词数上限：{role.word_limit}")
    if role.tone:
        lines.append(f"> 讲解风格：{role.tone}")
    if role.focus:
        lines.append(f"> 本篇重点：{role.focus}")
    if ctx.hits:
        lines.append(f"> 题材契合：命中 {len(ctx.hits)}/{len(ctx.role.keywords)} 个方向关键词 → {'、'.join(ctx.hits)}")
    return "\n".join(lines)


def _render_footer(ctx: _Ctx) -> str:
    role = ctx.role
    lines: list[str] = []
    if role.cta:
        lines += ["## 🎯 收尾任务", "", role.cta, ""]
    hint = role.tts_hint()
    source = f"母版 `{ctx.snap.story_id or '-'}`" + (f"（{Path(ctx.snap.path).name}）" if ctx.snap.path else "")
    lines += ["---", f"*EnglishFoundry：{source} × 角色 `{role.key}`（{ctx.level or '-'} / 上限 {role.word_limit} 词）*"]
    if hint:
        lines.append(f"*配音建议：{hint.get('tts_voice', '-')} · {hint.get('tts_rate', '-')}*")
    return "\n".join(lines)


def _assemble(ctx: _Ctx, details: Mapping[str, int]) -> str:
    blocks = [_render_header(ctx)]
    for module in ctx.modules:
        renderer = _SECTION_RENDERERS[module]
        block = renderer(ctx, details.get(module, 0))
        if block:
            blocks.append(block)
    blocks.append(_render_footer(ctx))
    return "\n\n".join(block.strip("\n") for block in blocks if block.strip()) + "\n"


def _truncate(markdown: str, ctx: _Ctx) -> str:
    """最后兜底：连“最小档”都塞不进预算时按词边界截断（正常角色配置走不到这里）。"""
    note = f"\n\n> ⚠️ 已按 **{ctx.role.name}** 的 {ctx.role.word_limit} 词上限截断，完整内容见母版 `{ctx.snap.story_id or '-'}`。"
    budget = max(1, ctx.role.word_limit - count_words(note))
    cut = _cut_words(markdown, budget, marker="").rstrip()
    if cut.count("<details>") > cut.count("</details>"):  # 别把答案折叠区撕成半个
        closer = "\n\n</details>"  # 补标签也算词数，得先从预算里扣掉
        cut = _cut_words(cut, max(1, budget - count_words(closer)), marker="").rstrip() + closer
    return cut + note + "\n"


def _fit(ctx: _Ctx) -> tuple[str, dict[str, int], bool]:
    """在角色预算里装进尽可能多的内容，返回 (markdown, 各板块档位, 是否硬截断)。

    两步走：先按 :meth:`Role.trim_order` 把最不该留的板块一档一档压到装得下；再反过来
    “回填”——把最后被砍的板块逐个还原一档，只要还在预算内就收下，避免一步砍过头、
    产出比预算小一大截。
    """
    limit = ctx.role.word_limit
    rank = {name: index for index, name in enumerate(ctx.trim or DEFAULT_TRIM_ORDER)}
    details = dict.fromkeys(ctx.modules, 0)
    markdown = _assemble(ctx, details)
    for _ in range(sum(MAX_DETAIL[name] for name in ctx.modules) + 2):
        used = count_words(markdown)
        if used <= limit:
            break
        movable = [name for name in ctx.modules if details[name] < MAX_DETAIL[name]]
        if not movable:
            log.warning("角色 %s 的 word_limit=%s 太小，产出已硬截断", ctx.role.key, limit)
            return _truncate(markdown, ctx), details, True
        victim = min(movable, key=lambda name: (rank.get(name, len(rank)), -details[name], name))
        details[victim] += 1
        log.debug("角色 %s 超预算（%s/%s 词），压缩「%s」到第 %s 档", ctx.role.key, used, limit, victim, details[victim])
        markdown = _assemble(ctx, details)
    if count_words(markdown) <= limit:
        details = _refill(ctx, details, rank)
        markdown = _assemble(ctx, details)
    return markdown, details, False


def _refill(ctx: _Ctx, details: dict[str, int], rank: Mapping[str, int]) -> dict[str, int]:
    """回填：最后被压缩的板块先还原一档，只要整篇还在预算内就保留这个结果。"""
    restored = dict(details)
    reduced = sorted((name for name, level in restored.items() if level > 0), key=lambda name: -rank.get(name, len(rank)))
    for name in reduced:
        while restored[name] > 0:
            trial = dict(restored)
            trial[name] -= 1
            if count_words(_assemble(ctx, trial)) <= ctx.role.word_limit:
                restored = trial
            else:
                break
    return restored


def render_for_role(
    pack: ContentPack | Mapping[str, Any],
    role_key: str,
    roles: Mapping[str, Role] | None = None,
    settings: Settings | None = None,
) -> str:
    """按角色渲染母版，返回 Markdown 字符串（整篇词数 ≤ 该角色的 ``word_limit``）。

    Args:
        pack: :class:`foundry.models.ContentPack`（或其 ``data`` 映射）。
        role_key: 角色 key，如 ``kids``、``ielts_toefl``（大小写/连字符不敏感）。
        roles: 预先加载好的角色表（批量渲染时传它，避免每个角色重读一次 yaml）。
        settings: 只在没传 ``roles`` 且需要定位 ``config/roles.yaml`` 时用。
    Raises:
        RolesError: 角色不存在或配置非法。
    """
    table = roles if roles is not None else load_roles(settings=settings)
    role = role_for(role_key, table)
    snap = _snapshot(pack)
    level, title, body, fallback = _pick_level(snap, role)
    modules = tuple(name for name in SECTION_ORDER if name in role.modules and snap.content_of(name))
    hits = _keyword_hits(role, snap, body)
    ctx = _Ctx(
        role=role,
        snap=snap,
        level=level,
        title=title,
        body=body,
        fallback=fallback,
        modules=modules,
        hits=hits,
        body_cap=role.body_words,
        trim=role.trim_order(modules),
    )
    if not snap.levels:
        log.debug("角色 %s：母版 %s 没有分级正文，只渲染可用板块", role.key, snap.story_id)

    markdown, _details, cut = _fit(ctx)
    if cut:
        log.warning("角色 %s：word_limit=%s 装不下核心板块，已按词边界截断", role.key, role.word_limit)
    return markdown


def render_all(
    pack: ContentPack | Mapping[str, Any],
    role_keys: Sequence[str] | None = None,
    roles: Mapping[str, Role] | None = None,
) -> dict[str, str]:
    """一次渲染多个角色（默认全部），返回 ``{role_key: markdown}``。"""
    table = roles if roles is not None else load_roles()
    keys = list(role_keys) if role_keys else list(table)
    return {key: render_for_role(pack, key, table) for key in keys}


# ---------------------------------------------------------------- 命令行
def _load_pack(source: Path) -> ContentPack:
    """读母版 JSON（``ContentPack.load`` 需要文件名当 story_id，所以只接受 .json）。"""
    return ContentPack.load(source)


def _main(argv: Sequence[str] | None = None) -> int:
    """看一眼角色表或渲染结果：``python -m foundry.roles --list`` / ``--role kids``"""
    parser = argparse.ArgumentParser(description="把母版 ContentPack 渲染成某角色的 Markdown（config/roles.yaml）")
    parser.add_argument("--list", action="store_true", help="打印全部角色配置摘要")
    parser.add_argument("--pack", metavar="PATH", help="母版 JSON（output/packs/*.json 或 fixture）")
    parser.add_argument("--role", default="kids", help="角色 key，逗号分隔多个（默认 kids）")
    parser.add_argument("--roles-file", metavar="PATH", help="角色配置（默认 config/roles.yaml）")
    parser.add_argument("--out", metavar="DIR", help="把渲染结果写成 <角色>.md 到该目录")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印压缩过程等 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        roles = load_roles(args.roles_file)
    except RolesError as error:
        print(f"{error}")
        return 1

    if args.list:
        print(f"共 {len(roles)} 个角色（{ROLES_FILE_NAME}）")
        for role in roles.values():
            print(f"  {role.summary()}")
        return 0

    if not args.pack:
        print("渲染需要 --pack 指向母版 JSON（或只用 --list 看角色表）")
        return 1

    source = Path(args.pack).expanduser()
    if not source.is_file():
        print(f"找不到母版文件：{source}")
        return 1
    pack = _load_pack(source)

    output_dir = Path(args.out).expanduser() if args.out else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for key in [item for item in re.split(r"[,\s]+", args.role) if item]:
        try:
            markdown = render_for_role(pack, key, roles)
        except RolesError as error:
            failures += 1
            print(f"{key}: 失败 -> {error}")
            continue
        print(f"[{key}] {count_words(markdown)} 词 / 上限 {role_for(key, roles).word_limit}，{len(markdown)} 字符")
        if output_dir is not None:
            target = output_dir / f"{key}.md"
            target.write_text(markdown, encoding="utf-8")
            print(f"    已写 {target}")
        else:
            print(markdown)
    return 0 if failures == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

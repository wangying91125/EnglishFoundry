"""``Story`` → ``ContentPack``：调用 OpenAI 兼容 LLM 生成“母版内容包”。

分工与约定：
    * 输入一个 Story（也接受 Article 或它们序列化后的映射），输出 :class:`foundry.models.ContentPack`；
      产物落盘在 ``output/packs/<story_id>.json``，键由 :data:`PACK_MODULES` 定义
      （levels / bilingual / vocabulary / grammar / background_zh / listening_script /
      speaking / writing / quiz）。
    * LLM 参数只来自 :class:`foundry.config.LLMSettings`（``base_url`` / ``api_key`` / ``model`` /
      ``temperature`` / ``timeout`` / ``max_retries``）：本模块不读环境变量、不写死 Key，
      缺 Key 时由 ``settings.llm.require_api_key()`` 给出可操作报错。
    * 提示词来自 ``prompts/master.md``（路径取 ``settings.prompts``），占位符写作 ``{{TITLE}}``
      这种形式（不用 str.format，因为正文里满是 JSON 花括号）；文件缺失时退化为
      :data:`FALLBACK_PROMPT`，保证临时项目根也能跑。
    * 出网只有一个入口 :func:`build_client`：装了 openai 就用官方 SDK 的
      ``chat.completions``，没装则用 requests 实现同款最小接口
      （``client.chat.completions.create``），所以离线环境照样能导入本模块、能注入假
      client 跑单测（:func:`generate_pack` 的 ``client`` 参数就是给测试和复用留的口子）。
    * 重试统一走 :func:`_with_retry`（tenacity 指数退避，缺失时内置退避循环）；
      JSON 解析不回来也算可重试失败——模型偶尔会加围栏或前后缀废话。
    * JSON 解析见 :func:`parse_json_response`：剥 ``` 围栏、扫出配平的第一个对象、
      ``strict=False`` 容忍串内裸换行、去掉尾逗号，实在不行才报错。
    * ``modules`` 参数挑选这次生成哪些键（None / ``"all"`` 表示全量，支持别名与逗号串），
      只保留被请求的键；缺键补空值，形状问题由 :func:`validate_pack` 汇总成告警，
      不让下游 roles / platforms 拿到结构完全不同的母版。

用法::

    from foundry.config import load_settings
    from foundry.master import generate_pack

    pack = generate_pack(story, load_settings())
    pack = generate_pack(story, modules=["levels", "vocabulary"])   # 只要两个键
    pack.data["levels"]["B1"]["title"]

手工跑一次：``python -m foundry.master --dry-run``（只渲染提示词，不调 LLM、不花钱）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from foundry.config import ConfigError, Settings, load_settings
from foundry.models import Article, ContentPack, Story, iso_or_none, stable_id

try:  # requests 是 openai SDK 缺失时的兜底通道
    import requests
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    requests = None

try:  # openai SDK 是声明依赖；缺失时用 requests 打同一套 /chat/completions
    from openai import APIError, APIConnectionError, APITimeoutError, OpenAI, RateLimitError
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    OpenAI = None
    _OPENAI_ERRORS: tuple[type[BaseException], ...] = ()
else:  # pragma: no cover - 取决于本机装了哪些包
    _OPENAI_ERRORS = (APIError, APIConnectionError, APITimeoutError, RateLimitError)

try:  # tenacity 是声明依赖；缺失时退化为内置退避循环
    from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    Retrying = None

__all__ = [
    "CEFR_LEVELS",
    "FALLBACK_PROMPT",
    "MASTER_PROMPT_FILE",
    "PACK_MODULES",
    "MasterError",
    "RetryableLLMError",
    "StoryView",
    "build_client",
    "build_messages",
    "chat_text",
    "clean_pack",
    "generate_pack",
    "load_prompt",
    "parse_json_response",
    "render_prompt",
    "resolve_modules",
    "story_view",
    "validate_pack",
]

log = logging.getLogger("foundry.master")

T = TypeVar("T")

# ---- 母版键（顺序 = 提示词里的顺序）----
PACK_MODULES: tuple[str, ...] = (
    "levels",
    "bilingual",
    "vocabulary",
    "grammar",
    "background_zh",
    "listening_script",
    "speaking",
    "writing",
    "quiz",
)
CEFR_LEVELS: tuple[str, ...] = ("A1", "A2", "B1", "B2", "C1", "C2")

#: 各键内部要求的字段（:func:`validate_pack` 与 :func:`clean_pack` 共用）
MODULE_FIELDS: dict[str, tuple[str, ...]] = {
    "bilingual": ("en", "zh"),
    "vocabulary": ("word", "pos", "zh", "en_def", "example"),
    "grammar": ("sentence", "point", "explanation_zh"),
}
#: 数量硬指标
MODULE_COUNTS: dict[str, int] = {"vocabulary": 12, "grammar": 3, "quiz": 5, "speaking.questions": 3}

MIN_BILINGUAL_SENTENCES = 3
BACKGROUND_MAX_CHARS = 150
LISTENING_MIN_WORDS = 80
ESSAY_MIN_WORDS, ESSAY_MAX_WORDS = 120, 200
MIN_QUIZ_CHOICES = 2

MAX_BODY_CHARS = 9_000  # 送给模型的正文字符上限（超出截断，避免爆上下文）
TRUNCATION_MARKER = " …（正文已截断）"
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 2.0
DEFAULT_TEMPERATURE = 0.4
MASTER_PROMPT_FILE = "master.md"
JSON_RESPONSE_FORMAT: dict[str, Any] = {"type": "json_object"}

SYSTEM_PROMPT = (
    "你是英语教研编辑，任务是按用户要求把一篇英文新闻加工成学习用的母版内容包。"
    "只输出一个 JSON 对象：不要解释、不要 Markdown 标题、不要代码围栏、不要输出未要求的键。"
)

FALLBACK_PROMPT = """把下面这篇英文新闻加工成母版内容包，只输出一个 JSON 对象，不要任何解释或代码围栏。

本次需要的键：{{MODULES}}

schema：
- levels: {{"A1"..."C2"}}，每档 {{"title": 英文改写标题, "body": 英文改写正文}}，难度必须真正分层
- bilingual: [{{"en": B1 版本原句, "zh": 中文翻译"}}]，逐句、按顺序
- vocabulary: 12 个 [{{"word", "pos", "zh", "en_def", "example"}}]，词必须出自正文
- grammar: 3 条长难句 [{{"sentence", "point", "explanation_zh"}}]
- background_zh: 简体中文背景，150 字以内
- listening_script: 由 B1 改写成的口语化听力稿（英文）
- speaking: {{"questions": 3 个讨论问题, "roleplay": {{"title", "scenario", "roles": 2 个角色, "task"}}}}
- writing: {{"topic": 范文题目, "essay": 120-200 词英文范文}}
- quiz: 5 题 [{{"q", "choices": 4 项, "answer": "A-D", "explain": 中文解析}}]

标题：{{TITLE}}
来源：{{SOURCE}} ｜ 分类：{{CATEGORY}} ｜ 时间：{{PUBLISHED}}
正文：
{{BODY}}
"""

#: modules 参数接受的别名（大小写、连字符、下划线都不敏感）
MODULE_ALIASES: dict[str, str] = {
    "level": "levels",
    "levels": "levels",
    "read": "levels",
    "reading": "levels",
    "bilingual": "bilingual",
    "sentence": "bilingual",
    "sentences": "bilingual",
    "vocab": "vocabulary",
    "vocabulary": "vocabulary",
    "words": "vocabulary",
    "grammar": "grammar",
    "background": "background_zh",
    "backgroundzh": "background_zh",
    "listening": "listening_script",
    "listen": "listening_script",
    "listeningscript": "listening_script",
    "speaking": "speaking",
    "speak": "speaking",
    "writing": "writing",
    "write": "writing",
    "essay": "writing",
    "quiz": "quiz",
    "question": "quiz",
    "questions": "quiz",
    "exercise": "quiz",
    "exercises": "quiz",
}

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_LETTERS = "ABCDEFGHIJ"  # 选择题选项顺序即 A/B/C/...

_EMPTY_CONTAINERS: dict[str, Any] = {
    "levels": dict,
    "speaking": dict,
    "writing": dict,
    "bilingual": list,
    "vocabulary": list,
    "grammar": list,
    "quiz": list,
    "background_zh": str,
    "listening_script": str,
}


class MasterError(RuntimeError):
    """母版生成彻底失败（不可重试，例如提示词缺失、响应不是对象、键名写错）。"""


class RetryableLLMError(MasterError):
    """值得重试的失败：传输层错误、限流、5xx、JSON 解析不出来。"""


# ---------------------------------------------------------------- 小工具
def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return ""
    return str(value).strip()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """从映射或对象上取字段（openai SDK 返回对象，requests 返回 dict，都要能用）。"""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _blank(value: Any) -> bool:
    """字段是否算空：空串、空列表、空对象都算（None 也算）。"""
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (Mapping, list, tuple)):
        return not value
    return value is None


def _records(value: Any) -> list[dict[str, Any]]:
    """把列表里的映射取出来（非映射项丢掉并记日志）。"""
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    items = [dict(item) for item in value if isinstance(item, Mapping)]
    dropped = len(list(value)) - len(items)
    if dropped:
        log.debug("忽略了 %s 个非对象条目", dropped)
    return items


def _words(text: str) -> int:
    return len((text or "").split())


def _norm_key(value: Any) -> str:
    """键名归一：小写、去空格/连字符，用于别名查表。"""
    return re.sub(r"[\s_\-]+", "", _as_text(value).lower())


# ---------------------------------------------------------------- 模块选择
def resolve_modules(modules: Any = None) -> tuple[str, ...]:
    """把 ``modules`` 参数解析成规范化的键元组。

    None / ``"all"`` / ``"*"`` 表示全量；也接受 ``"levels,vocabulary"`` 这种逗号串、
    别名（``vocab`` -> ``vocabulary``）。顺序永远按 :data:`PACK_MODULES`，未知名字直接报错。
    """
    if modules is None:
        return PACK_MODULES
    if isinstance(modules, str):
        text = modules.strip().lower()
        if text in {"", "all", "*", "any", "every", "全部", "所有"}:
            return PACK_MODULES
        requested: Iterable[Any] = [part for part in re.split(r"[,;、\s]+", text) if part]
    elif isinstance(modules, Mapping):
        requested = list(modules)
    else:
        requested = list(modules)

    selected: list[str] = []
    unknown: list[str] = []
    for item in requested:
        if isinstance(item, Mapping):  # 允许 [{"key": "levels"}] 这种写法
            item = item.get("key") or item.get("name")
        key = _norm_key(item)
        if not key:
            continue
        if key == "all":
            return PACK_MODULES
        canonical = MODULE_ALIASES.get(key)
        if canonical is None:
            unknown.append(_as_text(item))
            continue
        if canonical not in selected:
            selected.append(canonical)
    if unknown:
        raise MasterError(f"未知的模块名：{', '.join(unknown)}（可选：{', '.join(PACK_MODULES)}）")
    if not selected:
        raise MasterError(f"modules 解析后为空（可选：{', '.join(PACK_MODULES)}）")
    return tuple(key for key in PACK_MODULES if key in selected)


# ---------------------------------------------------------------- 输入整形
@dataclass
class StoryView:
    """喂给提示词的 Story 视图（Story / Article / 映射都能转进来）。"""

    id: str
    title: str = ""
    summary: str = ""
    body: str = ""
    source: str = ""
    category: str = "news"
    url: str = ""
    published: str = ""
    article_count: int = 0

    def to_placeholders(self) -> dict[str, str]:
        return {
            "STORY_ID": self.id,
            "TITLE": self.title or "(无标题)",
            "SUMMARY": self.summary or _summary_of(self.body),
            "BODY": self.body or self.summary or "(正文缺失)",
            "SOURCE": self.source or "-",
            "CATEGORY": self.category or "news",
            "URL": self.url or "-",
            "PUBLISHED": self.published or "-",
        }


def _summary_of(body: str, length: int = 320) -> str:
    text = " ".join((body or "").split())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def _coerce_story(story: Story | Article | Mapping[str, Any] | Any) -> Story:
    """把 Story / Article / 映射 / 鸭子对象统一成 Story（显式 id 一律沿用，文件名才稳定）。"""
    if story is None:
        raise MasterError("story 不能为空")
    if isinstance(story, Story):
        return story
    if isinstance(story, Article):
        item = Story(
            id=story.id,
            title=story.title,
            url=story.url,
            source=story.source,
            category=story.category,
            published=story.published,
            articles=[story],
        )
        item.summary = story.preview()
        return item
    if isinstance(story, Mapping):
        return Story.from_dict(story) if story.get("articles") is not None else _coerce_story(Article.from_dict(story))
    names = ("id", "title", "url", "source", "category", "published", "content")
    fields = {name: _field(story, name) for name in names}
    fields = {key: value for key, value in fields.items() if value not in (None, "")}
    if fields:
        return _coerce_story(Article(**fields))
    raise MasterError(f"无法识别的 story 输入：{type(story).__name__}")


def _join_bodies(articles: Sequence[Article]) -> str:
    """多来源 Story 把各篇正文并进来（同文转载只留一份），总量截断到 MAX_BODY_CHARS。"""
    parts: list[str] = []
    total = 0
    for index, article in enumerate(articles, start=1):
        text = " ".join((article.content or "").split())
        if not text or text in parts:
            continue
        label = f"[{article.source or f'来源{index}'}] " if len(articles) > 1 else ""
        chunk = label + text
        if total + len(chunk) > MAX_BODY_CHARS:
            room = MAX_BODY_CHARS - total - len(label) - len(TRUNCATION_MARKER)
            if room > 200:
                parts.append(label + text[:room].rstrip() + TRUNCATION_MARKER)
            log.debug("正文超过 %s 字符，已截断", MAX_BODY_CHARS)
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n".join(parts)


def story_view(story: Story | Article | Mapping[str, Any] | Any) -> StoryView:
    """把 Story / Article / dict 归一成 :class:`StoryView`（供提示词渲染使用）。"""
    item = _coerce_story(story)
    articles = list(item.articles)
    view = StoryView(
        id=item.id or stable_id(item.url or item.title),
        title=item.title or (articles[0].title if articles else ""),
        summary=item.summary or (articles[0].preview() if articles else ""),
        body=_join_bodies(articles),
        source=", ".join(item.sources) or (articles[0].source if articles else ""),
        category=item.category,
        url=item.url or (articles[0].url if articles else ""),
        published=iso_or_none(item.published) or "",
        article_count=len(articles),
    )
    if not view.title and not view.body:
        raise MasterError("Story 既没有标题也没有正文，无法生成母版")
    return view


# ---------------------------------------------------------------- 提示词
def _default_prompts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(name: str = MASTER_PROMPT_FILE, settings: Settings | None = None) -> str:
    """读取提示词模板（``settings.prompts`` 优先，其次包内相对的项目根）；都没有就用内置版。"""
    candidates: list[Path] = []
    if settings is not None:
        candidates.append(Path(settings.prompts) / name)
    candidates.append(_default_prompts_dir() / name)
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            log.debug("提示词：%s（%s 字符）", path, len(text))
            return text
        log.warning("提示词文件是空的：%s", path)
    log.warning("未找到提示词 %s，使用内置精简版（建议生成 %s）", name, MASTER_PROMPT_FILE)
    return FALLBACK_PROMPT


def render_prompt(template: str, view: StoryView, modules: Sequence[str] | None = None) -> str:
    """把 ``{{TOKEN}}`` 占位符换成 Story 内容（不用 str.format：正文里全是花括号）。"""
    selected = tuple(modules) if modules else PACK_MODULES
    table = view.to_placeholders()
    table["MODULES"] = "、".join(selected)
    table["LEVELS"] = "、".join(CEFR_LEVELS)
    rendered = template
    for token, value in table.items():
        rendered = rendered.replace(f"{{{{{token}}}}}", value)
    leftovers = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", rendered)))
    if leftovers:
        log.warning("提示词里有未替换的占位符：%s", ", ".join(leftovers))
    return rendered


def build_messages(prompt_text: str) -> list[dict[str, str]]:
    """system 只定纪律，user 带 schema 与原文。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt_text},
    ]


# ---------------------------------------------------------------- LLM 客户端
class _RestCompletions:
    """``client.chat.completions`` 的 requests 实现（openai SDK 不在时用）。"""

    def __init__(self, owner: "RestChatClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        if requests is None:  # pragma: no cover - 取决于本机装了哪些包
            raise MasterError("未安装 openai 也未安装 requests，无法调用 LLM：pip install openai")
        payload = {key: value for key, value in kwargs.items() if key not in ("timeout", "http_client")}
        timeout = kwargs.get("timeout") or self._owner.timeout
        headers = {
            "Authorization": f"Bearer {self._owner.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(self._owner.endpoint, headers=headers, json=payload, timeout=timeout)
        status = getattr(response, "status_code", 0)
        if status in RETRYABLE_STATUS:
            raise RetryableLLMError(f"HTTP {status}（可重试）：{response.text[:200]}")
        if status and status >= 400:
            raise MasterError(f"HTTP {status}：{response.text[:200]}")
        try:
            return response.json()
        except ValueError as error:
            raise RetryableLLMError(f"响应不是 JSON：{response.text[:200]}") from error


class _RestChat:
    def __init__(self, owner: "RestChatClient") -> None:
        self.completions = _RestCompletions(owner)


class RestChatClient:
    """OpenAI 兼容的最小客户端：``client.chat.completions.create(...)``。"""

    def __init__(self, *, base_url: str, api_key: str, timeout: float | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.chat = _RestChat(self)

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def close(self) -> None:  # pragma: no cover - requests 走模块级函数，无连接池需关闭
        return None


def build_client(settings: Settings | None = None) -> Any:
    """按 ``settings.llm`` 造 OpenAI 兼容客户端（SDK 优先，缺 SDK 用 requests 兜底）。"""
    settings = settings or load_settings()
    llm = settings.llm
    api_key = llm.require_api_key()  # 没 Key 就在这里给出可操作报错
    if OpenAI is not None:  # pragma: no cover - 取决于本机是否装了 openai
        # max_retries=0：重试统一由 _with_retry 负责，避免两层重试相乘
        return OpenAI(base_url=llm.base_url, api_key=api_key, timeout=llm.timeout, max_retries=0)
    if requests is None:  # pragma: no cover
        raise MasterError("未安装 openai 也未安装 requests：pip install openai 后重试")
    log.debug("openai SDK 不可用，使用 requests 兜底客户端 -> %s", llm.base_url)
    return RestChatClient(base_url=llm.base_url, api_key=api_key, timeout=float(llm.timeout))


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结果
            log.debug("关闭 LLM 客户端时出错", exc_info=True)


def _is_retryable(error: BaseException) -> bool:
    """有状态码就按状态码判断（4xx 不空耗），否则看异常类型。"""
    if isinstance(error, RetryableLLMError):
        return True
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS
    return isinstance(error, (OSError, *_OPENAI_ERRORS))


def _retry_reason(state: Any) -> str:
    outcome = getattr(state, "outcome", None)
    error = outcome.exception() if outcome is not None else None
    return f"{type(error).__name__}: {error}" if error else "未知错误"


def _with_retry(operation: Callable[[], T], *, attempts: int, backoff: float, label: str) -> T:
    """最多尝试 ``attempts`` 次、指数退避；装了 tenacity 就交给 tenacity（与 fetcher 同一口径）。"""
    total = max(1, _as_int(attempts, DEFAULT_ATTEMPTS))
    base = max(0.0, float(backoff or 0))
    if Retrying is not None:  # pragma: no cover - 取决于本机是否装了 tenacity
        retryer = Retrying(
            stop=stop_after_attempt(total),
            wait=wait_exponential(multiplier=base, max=max(base, base * 2 ** (total - 1))),
            retry=retry_if_exception_type((RetryableLLMError, OSError, *_OPENAI_ERRORS)),
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
        except Exception as error:  # noqa: BLE001 - 只重试可恢复错误，其余立刻抛出
            if attempt >= total or not _is_retryable(error):
                raise
            sleep_for = base * (2 ** (attempt - 1))
            log.warning("%s 第 %s 次失败（%.1f 秒后重试）：%s", label, attempt, sleep_for, error)
            if sleep_for:
                time.sleep(sleep_for)


# ---------------------------------------------------------------- 调用与解析
def extract_text(response: Any) -> str:
    """从 chat.completions 响应里取正文文本（对象/dict/分块 content 都兼容）。"""
    if response is None:
        raise RetryableLLMError("LLM 返回为空响应")
    if isinstance(response, str):
        return response
    message: Any = None
    choices = _field(response, "choices")
    if choices:
        message = _field(choices[0], "message") or _field(choices[0], "text")
        content = _field(message, "content")
    else:
        content = _field(response, "output_text")
        if content is None:
            raise RetryableLLMError(f"响应里没有 choices：{str(response)[:200]}")
    if isinstance(content, (list, tuple)):  # 多段 content：拼成一段文本
        pieces = [part if isinstance(part, str) else _field(part, "text", "") for part in content]
        content = "".join(_as_text(piece) for piece in pieces)
    if not _as_text(content):
        parsed = _field(message, "parsed")
        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=False)
        raise RetryableLLMError(f"响应的 message.content 为空：{str(response)[:200]}")
    return str(content)


def chat_text(
    messages: Sequence[Mapping[str, Any]],
    *,
    client: Any,
    model: str,
    temperature: float | None = DEFAULT_TEMPERATURE,
    timeout: float | None = None,
    max_tokens: int | None = None,
    response_format: Mapping[str, Any] | None = None,
    json_mode: bool = True,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """发一次 ``chat.completions`` 请求，拿回文本（不重试，重试是 :func:`generate_pack` 的事）。"""
    payload: dict[str, Any] = {"model": model, "messages": list(messages)}
    if json_mode:
        payload["response_format"] = dict(response_format or JSON_RESPONSE_FORMAT)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if timeout is not None:
        payload["timeout"] = timeout
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if extra:
        payload.update(dict(extra))

    completions = _field(_field(client, "chat"), "completions")
    if completions is None:
        raise MasterError("client 没有 chat.completions，请用 build_client() 或注入同结构的假客户端")
    try:
        response = completions.create(**payload)
    except MasterError:
        raise
    except Exception as error:  # noqa: BLE001 - 统一归类成可重试/不可重试
        if _is_retryable(error):
            raise RetryableLLMError(f"LLM 调用失败（可重试）：{type(error).__name__}: {error}") from error
        raise MasterError(f"LLM 调用失败：{type(error).__name__}: {error}") from error
    return extract_text(response)


_FENCE = re.compile(r"```(?:json|javascript|js|python)?[ \t]*\r?\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_fences(text: str) -> str:
    """去掉 ```json 围栏（模型很爱加），只留围栏内部。"""
    match = _FENCE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.startswith("```"):  # 围栏没闭合（响应被 max_tokens 截断）
        return re.sub(r"^```[ \t]*\w*[ \t]*", "", stripped).strip()
    return stripped


def _first_object(text: str) -> str:
    """扫出第一个配平的 ``{...}``（跳过字符串里的花括号），后面跟的废话一律丢掉。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]  # 配平失败（多半是被截断），交给解析环节报错或重试


def _loads(text: str) -> Any:
    """先严格解析，再 ``strict=False``（容忍串内裸换行），最后去尾逗号 / Python 字面量。"""
    for kwargs in ({"strict": True}, {"strict": False}):
        try:
            return json.loads(text, **kwargs)
        except (ValueError, TypeError):
            continue
    repaired = _TRAILING_COMMA.sub(r"\1", text)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    try:
        return json.loads(repaired, strict=False)
    except (ValueError, TypeError):
        return None


def parse_json_response(text: str) -> dict[str, Any]:
    """把 LLM 文本解析成 JSON 对象；解析不出来算可重试失败（模型偶尔加围栏或废话）。"""
    raw = _as_text(text)
    if not raw:
        raise RetryableLLMError("LLM 返回空文本")
    unfenced = _strip_fences(raw)
    candidates = [unfenced]
    object_text = _first_object(unfenced)
    if object_text and object_text != unfenced:
        candidates.append(object_text)
    brace = raw.find("{")
    if brace >= 0:
        candidates.append(raw[brace : raw.rfind("}") + 1])
    for candidate in candidates:
        data = _loads(candidate)
        if isinstance(data, Mapping):
            return {str(key): value for key, value in data.items()}
    raise RetryableLLMError(f"无法从响应里解析出 JSON 对象：{raw[:200]}…")


# ---------------------------------------------------------------- 结果整形
def _clean_levels(value: Any) -> dict[str, Any]:
    """levels 只保留 A1-C2（键名大写归一），每档补齐 title/body 字符串。"""
    items = value if isinstance(value, Mapping) else {}
    cleaned: dict[str, Any] = {}
    for key, entry in items.items():
        level = _as_text(key).upper()
        if level not in CEFR_LEVELS:
            log.debug("丢掉 levels 里不认识的分档：%s", key)
            continue
        source = entry if isinstance(entry, Mapping) else {}
        cleaned[level] = {"title": _as_text(source.get("title")), "body": _as_text(source.get("body"))}
    return {level: cleaned[level] for level in CEFR_LEVELS if level in cleaned}


def _normalize_answer(answer: Any, choices: Any) -> str:
    """把答案统一成大写字母：接受 "B" / "b" / "(B)" / "B. 选项" / 选项原文 / 从 1 起的序号。"""
    text = _as_text(answer)
    if not text:
        return ""
    options = choices if isinstance(choices, (list, tuple)) else []
    if len(text) == 1 and text.upper() in _LETTERS[: max(len(options), 1)]:
        return text.upper()
    head = re.match(r"^\s*[（(\[]?\s*([A-Ja-j])\s*[)）\].、:：]", text)
    if head:
        return head.group(1).upper()
    if text.isdigit() and 1 <= int(text) <= len(options):
        return _LETTERS[int(text) - 1]
    for index, option in enumerate(options):
        if _as_text(option).lower() == text.lower():
            return _LETTERS[index]
    return text


def _clean_quiz(value: Any) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in _records(value):
        entry = dict(item)
        choices = entry.get("choices")
        if isinstance(choices, (list, tuple)):
            entry["choices"] = [_as_text(option) for option in choices]
        entry["q"] = _as_text(entry.get("q") or entry.get("question"))
        entry["explain"] = _as_text(entry.get("explain") or entry.get("explanation"))
        entry["answer"] = _normalize_answer(entry.get("answer"), entry.get("choices"))
        cleaned.append(entry)
    return cleaned


def _clean_records(value: Any, fields: Sequence[str]) -> list[dict[str, Any]]:
    """bilingual / vocabulary / grammar：保证是对象列表，并把字符串字段擦干净。"""
    cleaned: list[dict[str, Any]] = []
    for item in _records(value):
        entry = dict(item)
        for field in fields:
            entry[field] = _as_text(entry.get(field))
        cleaned.append(entry)
    return cleaned


def clean_pack(data: Any, modules: Sequence[str] | None = None) -> dict[str, Any]:
    """按请求的键挑选并做最小幅度修复；缺的键补空值，保证 ``pack.keys()`` 可预期。"""
    if not isinstance(data, Mapping):
        raise MasterError("母版内容必须是 JSON 对象")
    selected = tuple(modules) if modules else PACK_MODULES
    payload = {str(key): value for key, value in data.items()}
    payload.pop("story_id", None)  # story_id 由 ContentPack 自己管

    result: dict[str, Any] = {}
    for key in selected:
        if key not in payload:
            log.warning("LLM 没有产出键 %s，已补空值", key)
            result[key] = _EMPTY_CONTAINERS.get(key, str)()
            continue
        value = payload[key]
        if key == "levels":
            result[key] = _clean_levels(value)
        elif key == "quiz":
            result[key] = _clean_quiz(value)
        elif key in MODULE_FIELDS:
            result[key] = _clean_records(value, MODULE_FIELDS[key])
        elif key in ("background_zh", "listening_script"):
            result[key] = _as_text(value)
        else:
            result[key] = value
    extra = [key for key in payload if key not in selected]
    if extra:
        log.debug("丢掉本次不需要的键：%s", ", ".join(extra))
    return result


# ---------------------------------------------------------------- 质量校验
def _check_levels(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["必须是对象"]
    problems: list[str] = []
    for level in CEFR_LEVELS:
        entry = value.get(level)
        if not isinstance(entry, Mapping):
            problems.append(f"缺 {level}")
            continue
        for field in ("title", "body"):
            if _blank(entry.get(field)):
                problems.append(f"{level}.{field} 为空")
    return problems


def _check_list(value: Any, fields: Sequence[str], expected: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return ["必须是数组"]
    problems: list[str] = []
    if expected is not None and len(value) != expected:
        problems.append(f"期望 {expected} 条，实际 {len(value)} 条")
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            problems.append(f"第 {index} 条不是对象")
            continue
        missing = [field for field in fields if _blank(item.get(field))]
        if missing:
            problems.append(f"第 {index} 条缺字段：{', '.join(missing)}")
    return problems


def _check_bilingual(value: Any) -> list[str]:
    problems = _check_list(value, MODULE_FIELDS["bilingual"])
    if isinstance(value, list) and len(value) < MIN_BILINGUAL_SENTENCES:
        problems.append(f"只有 {len(value)} 句，建议 ≥ {MIN_BILINGUAL_SENTENCES} 句")
    return problems


def _check_quiz(value: Any) -> list[str]:
    problems = _check_list(value, ("q", "choices", "answer", "explain"), MODULE_COUNTS["quiz"])
    if not isinstance(value, list):
        return problems
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            continue
        choices = item.get("choices")
        count = len(choices) if isinstance(choices, (list, tuple)) else 0
        if count < MIN_QUIZ_CHOICES:
            problems.append(f"第 {index} 题选项少于 {MIN_QUIZ_CHOICES} 个")
            continue
        answer = _as_text(item.get("answer")).upper()
        if answer and answer not in _LETTERS[:count]:
            problems.append(f"第 {index} 题答案 {item.get('answer')!r} 不在 A-{_LETTERS[count - 1]} 范围内")
    return problems


def _check_speaking(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["必须是对象"]
    problems: list[str] = []
    questions = value.get("questions")
    if not isinstance(questions, list):
        problems.append("questions 必须是数组")
    else:
        expected = MODULE_COUNTS["speaking.questions"]
        if len(questions) != expected:
            problems.append(f"questions 期望 {expected} 个，实际 {len(questions)} 个")
        if any(_blank(item) for item in questions):
            problems.append("questions 有空项")
    roleplay = value.get("roleplay")
    if not isinstance(roleplay, Mapping):
        problems.append("缺 roleplay 对象")
    else:
        missing = [field for field in ("title", "scenario", "roles", "task") if _blank(roleplay.get(field))]
        if missing:
            problems.append(f"roleplay 缺字段：{', '.join(missing)}")
    return problems


def _check_writing(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["必须是对象"]
    problems: list[str] = []
    if _blank(value.get("topic")):
        problems.append("缺 topic")
    essay = _as_text(value.get("essay"))
    if not essay:
        problems.append("缺 essay")
    elif not ESSAY_MIN_WORDS <= _words(essay) <= ESSAY_MAX_WORDS:
        problems.append(f"essay {_words(essay)} 词，应在 {ESSAY_MIN_WORDS}-{ESSAY_MAX_WORDS} 词")
    return problems


def _check_text(value: Any, *, max_chars: int | None = None, min_words: int = 0, label: str = "内容") -> list[str]:
    text = _as_text(value)
    if not text:
        return [f"{label} 为空"]
    problems: list[str] = []
    if max_chars is not None and len(text) > max_chars:
        problems.append(f"{label} {len(text)} 字，应 ≤ {max_chars} 字")
    if min_words and _words(text) < min_words:
        problems.append(f"{label} 仅 {_words(text)} 词，偏短（建议 ≥ {min_words} 词）")
    return problems


_MODULE_CHECKERS: dict[str, Callable[[Any], list[str]]] = {
    "levels": _check_levels,
    "bilingual": _check_bilingual,
    "vocabulary": lambda value: _check_list(value, MODULE_FIELDS["vocabulary"], MODULE_COUNTS["vocabulary"]),
    "grammar": lambda value: _check_list(value, MODULE_FIELDS["grammar"], MODULE_COUNTS["grammar"]),
    "background_zh": lambda value: _check_text(value, max_chars=BACKGROUND_MAX_CHARS, label="background_zh"),
    "listening_script": lambda value: _check_text(value, min_words=LISTENING_MIN_WORDS, label="listening_script"),
    "speaking": _check_speaking,
    "writing": _check_writing,
    "quiz": _check_quiz,
}


def validate_pack(data: Mapping[str, Any], modules: Sequence[str] | None = None) -> list[str]:
    """检查母版是否满足各键的数量/字段/长度指标，返回人类可读的问题清单（空列表=全合格）。"""
    selected = tuple(modules) if modules else PACK_MODULES
    problems: list[str] = []
    for key in selected:
        if key not in data:
            problems.append(f"{key}: 缺失")
            continue
        checker = _MODULE_CHECKERS.get(key)
        if checker is not None:
            problems.extend(f"{key}: {item}" for item in checker(data[key]))
    return problems


# ---------------------------------------------------------------- 主流程
def _pack_path(settings: Settings, story_id: str, path: str | Path | None = None) -> Path:
    target = Path(path) if path is not None else Path(settings.output.packs) / f"{story_id}.json"
    return target if target.is_absolute() else (settings.root / target).resolve()


def generate_pack(
    story: Story | Article | Mapping[str, Any],
    settings: Settings | None = None,
    modules: Any = None,
    *,
    client: Any = None,
    prompt: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    attempts: int | None = None,
    backoff: float | None = None,
    max_tokens: int | None = None,
    save: bool = True,
    path: str | Path | None = None,
) -> ContentPack:
    """生成一个 Story 的母版内容包，并（默认）保存为 ``output/packs/<story_id>.json``。

    Args:
        story: :class:`foundry.models.Story` / ``Article`` 或它们序列化后的映射。
        settings: 复用已加载的配置；None 时调用 :func:`foundry.config.load_settings`。
        modules: 本次生成哪些键（None/``"all"`` 全量；见 :func:`resolve_modules`）。
        client: 注入 OpenAI 兼容客户端（测试用假客户端；None 时由 :func:`build_client` 造）。
        prompt: 覆盖提示词模板（默认读 ``prompts/master.md``）。
        model / temperature / attempts / backoff / max_tokens: 覆盖 ``settings.llm`` 的对应项。
        save: 是否写盘；``path`` 指定落盘路径（默认 ``output/packs/<story_id>.json``）。

    Returns:
        :class:`foundry.models.ContentPack`，``data`` 只包含本次请求的键。

    Raises:
        MasterError: 输入/提示词有问题、模块名不认识，或重试耗尽后仍然失败。
        ConfigError: 没有 API Key（来自 ``settings.llm.require_api_key()``，发生在出网之前）。
    """
    settings = settings or load_settings()
    selected = resolve_modules(modules)
    view = story_view(story)
    llm = settings.llm
    label = f"母版生成 {view.id}"

    template = prompt if prompt is not None else load_prompt(settings=settings)
    messages = build_messages(render_prompt(template, view, selected))
    target = _pack_path(settings, view.id, path)

    owns_client = client is None
    active = client if client is not None else build_client(settings)
    try:
        data = _with_retry(
            lambda: parse_json_response(
                chat_text(
                    messages,
                    client=active,
                    model=_as_text(model) or llm.model,
                    temperature=llm.temperature if temperature is None else float(temperature),
                    timeout=float(llm.timeout),
                    max_tokens=max_tokens,
                )
            ),
            attempts=llm.max_retries if attempts is None else int(attempts),
            backoff=DEFAULT_BACKOFF if backoff is None else float(backoff),
            label=label,
        )
    finally:
        if owns_client:
            _close_client(active)

    pack = ContentPack(story_id=view.id, data=clean_pack(data, selected), path=target if save else None)
    problems = validate_pack(pack.data, selected)
    for problem in problems:
        log.warning("%s：%s", label, problem)
    if save:
        pack.write(target)
        log.info(
            "%s -> %s（%s 个键，%s 项待改进）",
            label,
            settings.rel(pack.path),
            len(pack.data),
            len(problems),
        )
    else:
        log.info("%s 完成（未保存，%s 个键）", label, len(pack.data))
    return pack


# ---------------------------------------------------------------- 命令行
def _load_stories(source: Path) -> list[Story]:
    """读 ``output/raw/stories.json``（列表或单个对象）。"""
    payload = json.loads(source.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else [payload]
    return [Story.from_dict(item) for item in items if isinstance(item, Mapping)]


def _main(argv: Sequence[str] | None = None) -> int:
    """跑一次母版生成：``python -m foundry.master --limit 1``（默认读 output/raw/stories.json）"""
    parser = argparse.ArgumentParser(description="用 LLM 把 Story 加工成母版内容包（output/packs/*.json）")
    parser.add_argument("--input", metavar="PATH", help="Story 清单 JSON（默认 output/raw/stories.json）")
    parser.add_argument("--limit", type=int, default=1, help="最多处理几个 Story（默认 1）")
    parser.add_argument("--modules", default=None, help="只生成这些键，逗号分隔（默认全量）")
    parser.add_argument("--dry-run", action="store_true", help="只渲染提示词并打印目标路径，不调用 LLM")
    parser.add_argument("--out", metavar="PATH", help="产物目录（默认 output/packs）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    source = Path(args.input) if args.input else settings.output.raw / "stories.json"
    if not source.is_absolute():
        source = (settings.root / source).resolve()
    if not source.is_file():
        print(f"没有输入文件：{settings.rel(source)}（先跑 python -m foundry.normalizer --save）")
        return 1

    try:
        selected = resolve_modules(args.modules)
    except MasterError as error:
        print(f"{error}")
        return 1

    stories = _load_stories(source)
    if args.limit and args.limit > 0:
        stories = stories[: args.limit]
    if not stories:
        print(f"{settings.rel(source)} 里没有 Story")
        return 1

    packs_dir = Path(args.out) if args.out else settings.output.packs
    if not packs_dir.is_absolute():
        packs_dir = (settings.root / packs_dir).resolve()
    template = load_prompt(settings=settings)
    failures = 0
    for index, story in enumerate(stories, start=1):
        view = story_view(story)
        target = _pack_path(settings, view.id, packs_dir / f"{view.id}.json")
        print(f"[{index}] {view.id} · {view.title or '(无标题)'}（{view.article_count} 篇 / 正文 {len(view.body)} 字符）")
        print(f"    将请求键：{', '.join(selected)}")
        if args.dry_run:
            rendered = render_prompt(template, view, selected)
            print(f"    提示词：{len(rendered)} 字符 -> 目标 {settings.rel(target)}（dry-run 未调用 LLM）")
            continue
        try:
            pack = generate_pack(story, settings, selected, path=target)
            problems = validate_pack(pack.data, selected)
            print(f"    已写 {settings.rel(pack.path)}（{len(pack.data)} 个键，{len(problems)} 项待改进）")
            for problem in problems[:5]:
                print(f"      · {problem}")
        except (MasterError, ConfigError) as error:  # 缺 Key / 重试耗尽都只记失败，不带 traceback
            failures += 1
            print(f"    失败：{error}")

    if args.dry_run:
        return 0
    return 0 if failures == 0 and stories else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

"""免费配音：用 edge-tts（微软 Edge 在线 TTS，免费、无需 Key、需要联网）把文本合成 mp3。

分工与约定：
    * 唯一出网点是 :func:`synth_one`：``edge_tts.Communicate(text, voice, rate=rate)`` 流式收
      音频字节，确认非空之后原子写盘（先写 ``xxx.mp3.part``，再 ``os.replace``），所以失败
      不会留下半个文件；长文本的分片由 edge-tts 内部完成，本模块只负责“一段文本 -> 一个 mp3”。
    * 口音与语速默认来自 :mod:`foundry.config`（``tts.voice`` = ``en-US-AriaNeural``、
      ``tts.rate`` = ``+0%``）；``voice`` 也接受别名 ``us_female`` / ``us_male`` /
      ``uk_female`` / ``au_female``，与 ``config/settings.yaml`` 以及 roles.yaml 里透传的
      ``tts_voice`` / ``tts_rate`` 提示直接对接（见 :data:`VOICES`）。
    * :func:`batch_synth` 是同步包装：一个事件循环里跑完整批（``asyncio.Semaphore`` 限并发），
      **单条失败只记日志并把错误留在** :class:`SynthResult` **里**，不影响其他条目，返回顺序
      与输入一致；``overwrite=False`` 时已有非空 mp3 的条目直接跳过（增量重跑）。
    * edge-tts 没装时导入本模块不报错（离线环境照样能跑单测、能看 ``--list-voices``），真正
      调用才抛 :class:`TtsUnavailable`；免费服务偶发抖动按 ``attempts`` 指数退避重试。
    * 角色 / 平台产物是 Markdown，直接念会读出“井号 星号 竖线”：:func:`speech_text` 默认先把
      它清成口播稿（去标题符、强调符、链接 URL、表格线、``<details>``、emoji），要逐字播报
      就传 ``strip_markup=False``。
    * 单测全程离线：``monkeypatch.setattr(foundry.tts, "edge_tts", 假模块)`` 换掉 ``Communicate``
      （因为本模块只在调用时才取 ``edge_tts.Communicate``，改真模块的属性也一样生效）。

用法::

    import asyncio

    from foundry.tts import batch_synth, synth_one

    path = asyncio.run(synth_one("Hello from EnglishFoundry", "output/audio/hello.mp3"))
    results = batch_synth({"hello": "Hello world", "bye": "Goodbye world"}, "output/audio")
    print([(r.key, r.ok, r.bytes) for r in results])

手工跑一次（需要能联网）：

    python -m foundry.tts --text "Hello from EnglishFoundry" --out output/audio/hello.mp3
    python -m foundry.tts --file output/posts/business/xiaohongshu.md --voice uk_female --rate "-10%"
    python -m foundry.tts --list-voices
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from foundry.config import DEFAULT_TTS_RATE, DEFAULT_TTS_VOICE, Settings, load_settings
from foundry.normalizer import clean_html

try:  # edge-tts 是联网配音的实现依赖；缺失时导入不报错，调用时才给出可操作提示
    import edge_tts
except ImportError:  # pragma: no cover - 取决于本机装了哪些包
    edge_tts = None

__all__ = [
    "AUDIO_SUFFIX",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_RATE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_VOICE",
    "DEFAULT_WORKERS",
    "MANIFEST_NAME",
    "VOICES",
    "SynthItem",
    "SynthResult",
    "TtsError",
    "TtsUnavailable",
    "batch_synth",
    "normalize_rate",
    "resolve_rate",
    "resolve_voice",
    "safe_name",
    "speech_text",
    "synth_one",
    "target_path",
    "write_manifest",
]

log = logging.getLogger("foundry.tts")

DEFAULT_VOICE = DEFAULT_TTS_VOICE  # en-US-AriaNeural
DEFAULT_RATE = DEFAULT_TTS_RATE  # +0%
AUDIO_SUFFIX = ".mp3"
MANIFEST_NAME = "manifest.json"
PART_SUFFIX = ".part"

#: 内置四个免费口音的别名（与 ``config/settings.yaml`` 的 ``tts.voices`` 一致）
VOICES: Mapping[str, str] = {
    "us_female": "en-US-AriaNeural",
    "us_male": "en-US-GuyNeural",
    "uk_female": "en-GB-SoniaNeural",
    "au_female": "en-AU-NatashaNeural",
}

DEFAULT_ATTEMPTS = 3  # 免费服务偶发抽风，重试两次很便宜
DEFAULT_BACKOFF = 1.5  # 第 n 次失败后等 backoff * n 秒
DEFAULT_TIMEOUT = 90.0  # 单条合成超时（秒）
DEFAULT_WORKERS = 3  # 批量并发数，1 为串行
MIN_AUDIO_BYTES = 128  # 小于这个字节数当成“没有有效音频”
MAX_KEY_CHARS = 80  # 输出文件名长度上限

# 口音名只要形如 en-US-AriaNeural / zh-CN-XiaoxiaoNeural / en-US-Ava:DragonHDNeural 即可；
# 真正的校验交给服务端，这里只挡掉带空格、明显是打错的东西，省一次无谓的出网。
VOICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-:][A-Za-z0-9]+)+$")
RATE_RE = re.compile(r"^([+-]?)(\d{1,3})\s*%?$")

# ---- Markdown -> 口播稿 ----
_MD_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_MD_HR = re.compile(r"^[ \t]*(?:[-*_]\s*){3,}$", re.M)
_MD_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
_MD_BULLET = re.compile(r"^([ \t]*)(?:[-*+]\s+|\d+[.)]\s+)", re.M)
_MD_TABLE_DIVIDER = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}[ \t]*(?:\|[ \t]*:?-+[ \t]*)*\|?[ \t]*$", re.M)
_MD_STRONG = re.compile(r"(?:\*\*|__)(?=\S)(.+?)(?<=\S)(?:\*\*|__)", re.S)
_MD_EM = re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)", re.S)
_MD_UNDER = re.compile(r"(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])")
_MD_STRAY = re.compile(r"[*~`]+")
_MD_LONE_HASH = re.compile(r"(?<=\s)#(?=\s)|^#(?=\s)")  # 表格里的“#”序号列，念出来是噪音
# · • ° ｜ 这类分隔符念出来只会添噪音（｜ 是角色页眉里用的全角竖线）
_PAUSE_SYMBOLS = str.maketrans({"\u00b7": " ", "\u2022": " ", "\u00b0": " ", "\uff5c": " "})
_URL = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.I)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d\u2190-\u21FF]+"
)
_UNSAFE_NAME = re.compile(r"[^\w\-. ]+")


class TtsError(RuntimeError):
    """配音失败（文本为空、口音非法、超时、拿不到音频……）。"""


class TtsUnavailable(TtsError):
    """没装 edge-tts，无法配音。"""


@dataclass(frozen=True)
class SynthItem:
    """一条待配音的文本。"""

    key: str = ""
    text: str = ""
    voice: str | None = None
    rate: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "text": self.text, "voice": self.voice, "rate": self.rate}


@dataclass(frozen=True)
class SynthResult:
    """一条配音的结果（失败也带着错误信息，供批量结果与清单使用）。"""

    key: str
    voice: str = DEFAULT_VOICE
    rate: str = DEFAULT_RATE
    path: Path | None = None
    ok: bool = False
    skipped: bool = False
    bytes: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def filename(self) -> str:
        return self.path.name if self.path else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "file": self.filename or None,
            "voice": self.voice,
            "rate": self.rate,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 2),
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error,
        }


# ---------------------------------------------------------------- 工具函数
def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick(source: Any, *names: str) -> Any:
    """从映射或对象上按候选字段名取值（第一个非空命中）。"""
    for name in names:
        if isinstance(source, Mapping):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value is not None and str(value).strip():
            return value
    return None


def _optional_text(value: Any) -> str | None:
    text = _as_text(value)
    return text or None


def _resolve(value: Any, settings: Settings) -> Path:
    """路径解析：绝对路径原样，相对路径以项目根为基准。"""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = settings.root / path
    return path.resolve()


def safe_name(name: Any) -> str:
    """把 key / 标题变成安全的文件名（不逃逸目录、不超长、去掉多余 .mp3）。"""
    text = Path(_as_text(name)).name
    text = re.sub(r"\.mp3$", "", text, flags=re.I)
    text = _UNSAFE_NAME.sub("-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._ ")
    return text[:MAX_KEY_CHARS].rstrip("-._ ")


def target_path(out_dir: str | Path, key: Any, *, suffix: str = AUDIO_SUFFIX) -> Path:
    """``out_dir`` + ``key`` -> 具体 mp3 路径。"""
    directory = Path(str(out_dir)).expanduser()
    return directory / f"{safe_name(key) or 'clip'}{suffix if suffix.startswith('.') else '.' + suffix}"


def resolve_voice(name: Any = None, settings: Settings | None = None) -> str:
    """口音名或别名 -> 真正可用的 voice；空值取配置默认。

    别名先查 :data:`VOICES`（内置四个），再查 ``settings.tts.voices``（配置同名项优先）；
    都不命中就原样返回，交给 :func:`_check_voice` 校验形状，好让 ``zh-CN-XiaoxiaoNeural``
    这类清单外的口音也能直接用。
    """
    presets: dict[str, str] = {**VOICES}
    if settings is not None:
        presets.update(settings.tts.voices)
    candidate = _as_text(name) or _as_text(settings.tts.voice if settings else DEFAULT_VOICE, DEFAULT_VOICE)
    return presets.get(candidate, candidate)


def normalize_rate(value: Any) -> str:
    """``"+0%"`` / ``"-10%"`` / ``"10"`` / ``0`` 统一成 edge-tts 要的 ``[+-]NN%``。"""
    text = _as_text(value)
    if not text:
        return DEFAULT_RATE
    match = RATE_RE.match(text)
    if not match:
        raise TtsError(f"语速格式不对：{text!r}（要写成 \"+0%\" / \"-10%\" / \"+5\" 这种百分比）")
    percent = _as_int(match.group(2), 0)
    if percent > 100:
        raise TtsError(f"语速超出范围：{text!r}（-100% ~ +100%）")
    sign = "-" if match.group(1) == "-" else "+"
    return f"{sign}{percent}%"


def resolve_rate(value: Any = None, settings: Settings | None = None) -> str:
    """语速解析（``"+0%"``），空值取配置默认。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return normalize_rate(settings.tts.rate if settings else DEFAULT_RATE)
    return normalize_rate(value)


def _check_voice(voice: str) -> str:
    """结构上校验口音名，非法直接报错（省一次出网，报错比服务端友好）。"""
    text = _as_text(voice)
    if not text or len(text) > 64 or not VOICE_RE.match(text):
        raise TtsError(
            f"口音名不合法：{voice!r}（形如 en-US-AriaNeural；别名可选 {', '.join(sorted(VOICES))}；"
            f"完整清单见 edge-tts --list-voices）"
        )
    return text


def speech_text(text: Any) -> str:
    """Markdown / HTML -> 适合朗读的纯文本（整篇压成一行，停顿交给 edge-tts 按句子处理）。

    去掉标题符、强调符、列表符、表格线与分隔线、``<details>`` 之类的标签、链接 URL（保留锚文
    本）、emoji、箭头与 ``·｜`` 这类分隔符；曲线引号/破折号由
    :func:`foundry.normalizer.clean_html` 统一成 ASCII，空白一并压干净。
    """
    raw = text if isinstance(text, str) else _as_text(text)
    if not raw.strip():
        return ""
    cleaned = _MD_FENCE.sub(lambda match: match.group(1), raw)
    cleaned = _MD_LINK.sub(lambda match: match.group(1), cleaned)
    cleaned = _MD_HEADING.sub("", cleaned)
    cleaned = _MD_HR.sub("", cleaned)
    cleaned = _MD_QUOTE.sub("", cleaned)
    cleaned = _MD_TABLE_DIVIDER.sub("", cleaned)
    cleaned = _MD_BULLET.sub(lambda match: match.group(1), cleaned)
    cleaned = cleaned.replace("|", " ")
    cleaned = _MD_LONE_HASH.sub(" ", cleaned)
    cleaned = _MD_STRONG.sub(lambda match: match.group(1), cleaned)
    cleaned = _MD_EM.sub(lambda match: match.group(1), cleaned)
    cleaned = _MD_UNDER.sub(lambda match: match.group(1), cleaned)
    cleaned = _MD_STRAY.sub(" ", cleaned)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _EMOJI.sub(" ", cleaned)
    cleaned = cleaned.translate(_PAUSE_SYMBOLS)
    return clean_html(cleaned, paragraphs=False)


def _verbatim(value: Any) -> str:
    """逐字播报用的文本：只做类型归一，不动一个字符。"""
    return "" if value is None else str(value)


def _is_audio(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > MIN_AUDIO_BYTES
    except OSError:  # pragma: no cover - 权限/坏符号链接等
        return False


def _write_audio(path: Path, payload: bytes) -> None:
    """原子写盘：先 ``.part`` 再 ``os.replace``，读者永远拿不到半个 mp3。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + PART_SUFFIX)
    try:
        temp.write_bytes(payload)
        os.replace(temp, path)
    finally:
        if temp.exists():  # pragma: no cover - 只在写盘炸到时走到
            temp.unlink(missing_ok=True)


def _communicate(text: str, voice: str, rate: str) -> Any:
    """建 ``edge_tts.Communicate``（本模块唯一使用 edge_tts 的地方，单测 monkeypatch 它）。"""
    if edge_tts is None:
        raise TtsUnavailable("未安装 edge-tts，无法配音：pip install \"edge-tts>=6.1.0\"（配音需要能联网）")
    factory = getattr(edge_tts, "Communicate", None)
    if factory is None:
        raise TtsUnavailable("edge_tts.Communicate 不存在：请升级 edge-tts（pip install -U \"edge-tts>=6.1.0\"）")
    return factory(text, voice=voice, rate=rate)


def _chunk_audio(chunk: Any) -> bytes:
    """从 stream() 的一个块里取音频字节；WordBoundary 之类的非音频块返回空。

    edge-tts 的块类型是 `str` 枚举（``DataType.AUDIO``），所以先取 ``.value`` 再比字符串；
    字典块和对象块两种形态都吃。
    """
    if isinstance(chunk, Mapping):
        kind, data = chunk.get("type"), chunk.get("data")
    else:
        kind, data = getattr(chunk, "type", None), getattr(chunk, "data", None)
    if _as_text(getattr(kind, "value", kind)).lower() != "audio":
        return b""
    return bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else b""


async def _collect_audio(communicate: Any, *, timeout: float | None) -> bytes:
    """把 ``communicate.stream()`` 里的音频块拼成完整 mp3 字节（可带总超时）。"""

    async def drain() -> bytes:
        parts: list[bytes] = []
        async for chunk in communicate.stream():
            payload = _chunk_audio(chunk)
            if payload:
                parts.append(payload)
        return b"".join(parts)

    if timeout and timeout > 0:
        return await asyncio.wait_for(drain(), timeout)
    return await drain()


async def _synthesize(
    body: str,
    voice: str,
    rate: str,
    *,
    attempts: int,
    backoff: float,
    timeout: float | None,
    label: str,
) -> bytes:
    """带退避重试的一次合成；重试后仍失败统一抛 :class:`TtsError`。"""
    rounds = max(1, _as_int(attempts, 1))
    last: BaseException | None = None
    for attempt in range(1, rounds + 1):
        try:
            payload = await _collect_audio(_communicate(body, voice, rate), timeout=timeout)
            if not payload:
                raise TtsError("edge-tts 没有返回音频（文本可能不适合朗读，或被服务限流）")
            return payload
        except TtsUnavailable:
            raise
        except asyncio.TimeoutError as error:
            last = TimeoutError(f"{timeout:g}s 内没合成完")
            log.warning("配音超时：%s（第 %s/%s 次）", label, attempt, rounds)
        except Exception as error:  # noqa: BLE001 - 免费服务的异常形态很多，一律按可重试处理
            last = error
            log.warning("配音失败：%s（第 %s/%s 次）%s: %s", label, attempt, rounds, type(error).__name__, error)
        if attempt < rounds and backoff > 0:
            await asyncio.sleep(backoff * attempt)
    reason = f"{type(last).__name__}: {last}" if last else "未知错误"
    raise TtsError(f"配音失败（{label}，试了 {rounds} 次）：{reason}") from last


# ---------------------------------------------------------------- 对外接口
async def synth_one(
    text: Any,
    out_path: str | Path,
    voice: str | None = None,
    rate: str | None = None,
    *,
    settings: Settings | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    timeout: float | None = DEFAULT_TIMEOUT,
    strip_markup: bool = True,
    overwrite: bool = True,
) -> Path:
    """异步合成一条文本到 ``out_path``（mp3），返回最终路径。

    Args:
        text: 要朗读的文本；角色 / 平台 Markdown 也可以，默认先 :func:`speech_text` 清洗。
        out_path: 目标 mp3 路径；父目录自动创建，相对路径按项目根解析。
        voice: 口音名（``en-US-GuyNeural``）或别名（``us_male``）；None 用 ``tts.voice``。
        rate: 语速（``"+0%"`` / ``"-10%"``）；None 用 ``tts.rate``。
        settings: 复用已加载的配置；None 时调用 :func:`foundry.config.load_settings`。
        attempts: 单条最多试几次（免费服务偶发抽风）。
        backoff: 重试间隔基数（秒），第 n 次失败后等 ``backoff * n``；0 为立刻重试。
        timeout: 单次合成超时秒数，None 或 <=0 表示不限。
        strip_markup: False 时逐字播报，不做 Markdown 清洗。
        overwrite: False 且目标已是非空 mp3 就直接返回原文件，不再合成。

    Raises:
        TtsUnavailable: 没装 edge-tts。
        TtsError: 文本为空、口音/语速非法、超时或重试后仍拿不到音频。
    """
    loaded = settings if settings is not None else load_settings()
    target = _resolve(out_path, loaded)
    if not overwrite and _is_audio(target):
        log.debug("已存在非空音频，跳过：%s", loaded.rel(target))
        return target

    body = speech_text(text) if strip_markup else _verbatim(text)
    if not body.strip():
        raise TtsError(f"没有可朗读的文本，无法生成 {target.name}")
    chosen_voice = _check_voice(resolve_voice(voice, loaded))
    chosen_rate = resolve_rate(rate, loaded)

    payload = await _synthesize(
        body,
        chosen_voice,
        chosen_rate,
        attempts=attempts,
        backoff=backoff,
        timeout=timeout,
        label=target.name,
    )
    _write_audio(target, payload)
    log.info(
        "配音完成：%s（%s 字节 / %s / %s）",
        loaded.rel(target),
        f"{len(payload):,}",
        chosen_voice,
        chosen_rate,
    )
    return target


def _coerce_item(raw: Any, index: int, *, key_hint: Any = None) -> SynthItem:
    """把各种形状（SynthItem / 字符串 / 二元组 / 映射 / 带字段的对象）变成 :class:`SynthItem`。"""
    if isinstance(raw, SynthItem):
        item = replace(raw, key=_as_text(key_hint) or raw.key) if key_hint else raw
    elif isinstance(raw, str):
        item = SynthItem(key=_as_text(key_hint), text=raw)
    elif isinstance(raw, Mapping):
        item = SynthItem(
            key=_as_text(key_hint) or _as_text(_pick(raw, "key", "name", "id", "slug", "file")),
            text=_as_text(_pick(raw, "text", "content", "body", "markdown")),
            voice=_optional_text(_pick(raw, "voice", "tts_voice")),
            rate=_optional_text(_pick(raw, "rate", "tts_rate")),
        )
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and raw:
        parts = list(raw)[:4]
        if len(parts) == 1:
            item = SynthItem(key=_as_text(key_hint), text=_as_text(parts[0]))
        else:
            item = SynthItem(
                key=_as_text(parts[0]),
                text=_as_text(parts[1]),
                voice=_optional_text(parts[2]) if len(parts) > 2 else None,
                rate=_optional_text(parts[3]) if len(parts) > 3 else None,
            )
    else:
        item = SynthItem(
            key=_as_text(key_hint) or _as_text(_pick(raw, "key", "id", "slug", "name", "file")),
            text=_as_text(_pick(raw, "text", "content", "body", "markdown")),
            voice=_optional_text(_pick(raw, "voice", "tts_voice")),
            rate=_optional_text(_pick(raw, "rate", "tts_rate")),
        )
    key = safe_name(item.key) or f"clip-{index + 1:02d}"
    return replace(item, key=key)


def _coerce_items(items: Any) -> list[SynthItem]:
    """批量输入归一化；空 key 用 ``clip-01`` 这种序号补上，保证一条一个文件。"""
    if items is None:
        return []
    if isinstance(items, (SynthItem, str, bytes)):
        return [_coerce_item(items, 0)]
    if isinstance(items, Mapping):
        return [_coerce_item(text, index, key_hint=key) for index, (key, text) in enumerate(items.items())]
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)) and items:
        first = items[0]
        # ["a", "b"] 这种“纯文本列表”也别让用户写 key
        if isinstance(first, str):
            return [_coerce_item(text, index) for index, text in enumerate(items)]
    return [_coerce_item(raw, index) for index, raw in enumerate(items)]


async def _batch_async(
    plan: Sequence[SynthItem],
    out_dir: Path,
    loaded: Settings,
    *,
    voice: str | None,
    rate: str | None,
    workers: int,
    attempts: int,
    backoff: float,
    timeout: float | None,
    strip_markup: bool,
    overwrite: bool,
) -> list[SynthResult]:
    limiter = asyncio.Semaphore(max(1, workers))

    async def one(index: int, item: SynthItem) -> SynthResult:
        started = time.perf_counter()
        raw_voice = item.voice or voice
        raw_rate = item.rate or rate
        fallback = SynthResult(
            key=item.key or f"clip-{index + 1:02d}",
            voice=_as_text(resolve_voice(raw_voice, loaded), DEFAULT_VOICE),
            rate=_as_text(raw_rate or loaded.tts.rate, DEFAULT_RATE),
        )
        target = target_path(out_dir, item.key)
        try:
            chosen_voice = _check_voice(resolve_voice(raw_voice, loaded))
            chosen_rate = resolve_rate(raw_rate, loaded)
            async with limiter:
                if not overwrite and _is_audio(target):
                    size = target.stat().st_size
                    log.info("已存在，跳过：%s", loaded.rel(target))
                    return replace(fallback, voice=chosen_voice, rate=chosen_rate, path=target, ok=True, skipped=True, bytes=size)
                path = await synth_one(
                    item.text,
                    target,
                    chosen_voice,
                    chosen_rate,
                    settings=loaded,
                    attempts=attempts,
                    backoff=backoff,
                    timeout=timeout,
                    strip_markup=strip_markup,
                    overwrite=True,
                )
        except Exception as error:  # noqa: BLE001 - 单条失败不能带走整批
            log.warning("条目 %s 配音失败：%s", item.key, error)
            return replace(fallback, seconds=time.perf_counter() - started, error=f"{type(error).__name__}: {error}")
        size = path.stat().st_size if path.exists() else 0
        return replace(
            fallback,
            voice=chosen_voice,
            rate=chosen_rate,
            path=path,
            ok=True,
            bytes=size,
            seconds=time.perf_counter() - started,
        )

    return list(await asyncio.gather(*(one(index, item) for index, item in enumerate(plan))))


def _run(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """同步包装：不在事件循环里就 ``asyncio.run``；在循环里就明确报错，让调用方改用 await。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    raise TtsError(
        "batch_synth() 不能在已运行的事件循环里调用：请 await synth_one(...)，"
        "或用 asyncio.to_thread(batch_synth, ...) / run_in_executor"
    )


def batch_synth(
    items: Any,
    out_dir: str | Path | None = None,
    *,
    settings: Settings | None = None,
    voice: str | None = None,
    rate: str | None = None,
    workers: int | None = None,
    attempts: int | None = None,
    backoff: float | None = None,
    timeout: float | None = None,
    strip_markup: bool = True,
    overwrite: bool = True,
) -> list[SynthResult]:
    """批量配音（同步包装），返回与输入等长、同序的 :class:`SynthResult` 列表。

    Args:
        items: 多种输入形状都接受——``{"文件名": "文本"}`` 映射、``[("key", "文本"), ...]``
            二元组、纯字符串列表、``SynthItem`` 列表，或带 ``key``/``text``/``voice``/``rate``
            （或 ``tts_voice``/``tts_rate``）字段的对象/映射（角色渲染结果直接传就行）。
        out_dir: 输出目录；None 用 ``settings.output.audio``（相对路径按项目根解析）。
        voice / rate: 整批默认口音与语速；条目自带的值优先。
        workers: 并发数，默认 :data:`DEFAULT_WORKERS`；1 为串行。
        attempts / backoff / timeout: 逐条透传给 :func:`synth_one`。
        strip_markup: 是否把 Markdown 清成口播稿（默认 True）。
        overwrite: False 时已有非空 mp3 的条目跳过（结果里 ``skipped=True``）。

    Returns:
        每条一个结果：成功带着路径与字节数，失败带着错误信息（**不影响其他条目**）。
    """
    loaded = settings if settings is not None else load_settings()
    plan = _coerce_items(items)
    if not plan:
        log.warning("batch_synth：没有要配音的条目")
        return []
    directory = _resolve(out_dir if out_dir is not None else loaded.output.audio, loaded)
    directory.mkdir(parents=True, exist_ok=True)

    seen: dict[str, int] = {}
    for item in plan:
        seen[item.key] = seen.get(item.key, 0) + 1
    duplicated = sorted(key for key, count in seen.items() if count > 1)
    if duplicated:  # 同名条目会互相覆盖，提前提醒（仍按顺序全部合成）
        log.warning("以下条目的输出文件名重复，后者覆盖前者：%s", ", ".join(duplicated))

    results = _run(
        lambda: _batch_async(
            plan,
            directory,
            loaded,
            voice=voice,
            rate=rate,
            workers=_as_int(workers, DEFAULT_WORKERS) or 1,
            attempts=_as_int(attempts, DEFAULT_ATTEMPTS) or 1,
            backoff=_as_float(backoff, DEFAULT_BACKOFF) if backoff is not None else DEFAULT_BACKOFF,
            timeout=_as_float(timeout, DEFAULT_TIMEOUT) if timeout is not None else DEFAULT_TIMEOUT,
            strip_markup=strip_markup,
            overwrite=overwrite,
        )
    )
    ok = sum(1 for result in results if result.ok)
    failed = len(results) - ok
    log.info(
        "批量配音完成：%s/%s 成功（输出 %s，失败 %s）",
        ok,
        len(results),
        loaded.rel(directory),
        ", ".join(result.key for result in results if not result.ok) or "-",
    )
    return list(results)


def write_manifest(
    results: Iterable[SynthResult],
    path: str | Path | None = None,
    *,
    settings: Settings | None = None,
) -> Path:
    """把批量结果写成 ``manifest.json``（给 site / pipeline 反查音频用）。"""
    loaded = settings if settings is not None else load_settings()
    rows = list(results)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "voice": loaded.tts.voice,
        "rate": loaded.tts.rate,
        "total": len(rows),
        "succeeded": sum(1 for row in rows if row.ok),
        "skipped": sum(1 for row in rows if row.skipped),
        "failed": sum(1 for row in rows if not row.ok),
        "items": [row.to_dict() for row in rows],
    }
    target = _resolve(path if path is not None else loaded.output.audio / MANIFEST_NAME, loaded)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("清单已写入 %s", loaded.rel(target))
    return target


# ---------------------------------------------------------------- 命令行
def _read_source(spec: str, loaded: Settings) -> tuple[str, str]:
    """读一个文本来源（``-`` 为 stdin），返回 (key, 文本)。"""
    if spec == "-":
        return "stdin", sys.stdin.read()
    source = _resolve(spec, loaded)
    if not source.is_file():
        raise TtsError(f"文件不存在：{source}")
    return source.stem, source.read_text(encoding="utf-8", errors="replace")


def _main(argv: Sequence[str] | None = None) -> int:
    """命令行跑一次配音，方便验收：``python -m foundry.tts --text "..." --out ...``"""
    parser = argparse.ArgumentParser(description="用 edge-tts（微软，免费，需联网）把文本合成 mp3")
    parser.add_argument("--text", action="append", default=[], metavar="TEXT", help="要配音的文本，可重复")
    parser.add_argument("--file", action="append", default=[], metavar="PATH", help="从文件读文本（- 为 stdin），可重复")
    parser.add_argument("--voice", default=None, help=f"口音名或别名（{', '.join(VOICES)}），默认取配置 tts.voice")
    parser.add_argument("--rate", default=None, help='语速，如 "-10%%"（写作 --rate=-10%%）')
    parser.add_argument("--out", metavar="PATH", default=None, help="只有一条时写到这个 mp3")
    parser.add_argument("--out-dir", metavar="DIR", default=None, help="批量输出目录（默认 output/audio）")
    parser.add_argument("--workers", type=int, default=None, help=f"并发数，默认 {DEFAULT_WORKERS}；1 为串行")
    parser.add_argument("--attempts", type=int, default=None, help=f"单条最多试几次，默认 {DEFAULT_ATTEMPTS}")
    parser.add_argument("--timeout", type=float, default=None, help=f"单条超时秒数，默认 {DEFAULT_TIMEOUT:g}")
    parser.add_argument("--keep-markup", action="store_true", help="不清洗 Markdown，逐字送给 TTS")
    parser.add_argument("--skip-existing", action="store_true", help="目标已有非空 mp3 就跳过（增量重跑）")
    parser.add_argument("--manifest", nargs="?", const="", metavar="PATH", help="写清单 JSON（默认 output/audio/manifest.json）")
    parser.add_argument("--list-voices", action="store_true", help="打印内置口音别名与默认语速后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    loaded = load_settings()

    if args.list_voices:
        print(f"默认：{loaded.tts.voice} · {loaded.tts.rate}（别名 -> edge-tts 口音，需 pip install edge-tts 且能联网）")
        for alias, name in VOICES.items():
            print(f"  {alias:<10} {name}")
        for key, value in loaded.tts.voices.items():
            if VOICES.get(key) != value:
                print(f"  {key:<10} {value}（来自 config/settings.yaml）")
        return 0

    items: list[SynthItem] = [
        SynthItem(key=f"clip-{index:02d}", text=text, voice=args.voice, rate=args.rate)
        for index, text in enumerate(args.text, start=1)
    ]
    for spec in args.file:
        try:
            key, body = _read_source(spec, loaded)
        except TtsError as error:
            print(str(error), file=sys.stderr)
            return 1
        items.append(SynthItem(key=key, text=body, voice=args.voice, rate=args.rate))
    if not items:
        print("至少要一条 --text 或 --file（试试 --list-voices 看口音）", file=sys.stderr)
        return 2

    single_dir: Path | None = None
    if args.out:
        if len(items) != 1:
            print("--out 只支持一条文本；多条请用 --out-dir", file=sys.stderr)
            return 2
        single = _resolve(args.out, loaded)
        if single.suffix.lower() != AUDIO_SUFFIX:
            log.warning("--out 的扩展名改成 %s（edge-tts 只出 mp3）", AUDIO_SUFFIX)
            single = single.with_suffix(AUDIO_SUFFIX)
        items[0] = replace(items[0], key=single.stem)
        single_dir = single.parent

    out_dir = _resolve(single_dir if single_dir is not None else (args.out_dir or loaded.output.audio), loaded)
    try:
        results = batch_synth(
            items,
            out_dir,
            settings=loaded,
            workers=args.workers,
            attempts=args.attempts,
            timeout=args.timeout,
            strip_markup=not args.keep_markup,
            overwrite=not args.skip_existing,
        )
    except TtsError as error:
        print(str(error), file=sys.stderr)
        return 1

    width = max(len(row.key) for row in results)
    succeeded = sum(1 for row in results if row.ok)
    print(f"配音：{succeeded}/{len(results)} 成功 -> {loaded.rel(out_dir)}")
    for row in results:
        mark = "SKIP" if row.skipped else ("OK  " if row.ok else "FAIL")
        where = loaded.rel(row.path) if row.path else "-"
        print(f"  {mark} {row.key:<{width}} {row.voice} {row.rate:>5} {row.bytes:>8,}B {row.seconds:5.1f}s {where}")
        if row.error:
            print(f"       错误：{row.error}")

    if args.manifest is not None:
        path = write_manifest(results, args.manifest or out_dir / MANIFEST_NAME, settings=loaded)
        print(f"清单已写入 {loaded.rel(path)}")
    return 0 if succeeded == len(results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

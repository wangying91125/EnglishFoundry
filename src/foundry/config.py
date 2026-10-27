"""配置加载：合并 ``config/*.yaml``、项目根 ``.env`` 与环境变量。

优先级（高 → 低）：环境变量 > ``.env`` > ``config/*.yaml`` > 代码内置默认值。

约定：
    * 所有路径都以项目根为基准；配置里可以写相对路径，读取后一律解析为绝对路径。
    * 仓库里不写死任何密钥。配置文件只允许 ``${VAR}`` / ``${VAR:-默认值}`` 占位符，
      真实 Key 只来自环境变量或 ``.env``（``.env`` 已被 .gitignore 忽略）。
    * 后续模块统一通过 :func:`load_settings` 取配置，不要各自读 yaml。

用法::

    from foundry.config import load_settings

    settings = load_settings()
    settings.llm.base_url      # OpenAI 兼容 endpoint
    settings.tts.voice         # edge-tts 默认口音
    settings.output.packs      # output/packs 绝对路径
    settings.enabled_sources() # config/sources.yaml 中启用的源
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping

import yaml

try:  # python-dotenv 是声明依赖；万一没装，退化为内置解析器，保证本模块永远可用
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover
    _load_dotenv = None

__all__ = [
    "ConfigError",
    "FetchSettings",
    "LLMSettings",
    "NormalizeSettings",
    "OutputDirs",
    "Settings",
    "TTSSettings",
    "find_project_root",
    "load_config_dir",
    "load_settings",
]

log = logging.getLogger("foundry.config")

# ---- 环境变量名 ----
ENV_API_KEY = "DASHSCOPE_API_KEY"
ENV_BASE_URL = "DASHSCOPE_BASE_URL"
ENV_MODEL = "FOUNDRY_MODEL"
ENV_ROOT = "FOUNDRY_ROOT"

# ---- 内置默认值（不含任何密钥）----
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
DEFAULT_TTS_VOICE = "en-US-AriaNeural"
DEFAULT_TTS_RATE = "+0%"
DEFAULT_USER_AGENT = "EnglishFoundry/1.0 (+https://github.com)"

# ``.env.example`` 里的示例值：直接 cp 成 .env 而没填 Key 时，不能当成真实 Key
PLACEHOLDER_TOKENS = ("在此填入你的百炼Key", "your-api-key", "your_api_key_here", "changeme", "sk-xxx", "xxx")

_PLACEHOLDER = re.compile(r"^\$\{(\w+)(?::-([^}]*))?\}$")


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


# ---------------------------------------------------------------- 工具函数
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


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _env_first(name: str) -> str:
    """读取环境变量，未设置返回空串。"""
    return (os.environ.get(name) or "").strip()


def _clean_api_key(value: Any) -> tuple[str, bool]:
    """拆成 (真实 Key, 是否为示例占位值)，避免没填过的 .env 伪装成“已配置”。"""
    text = _as_text(value)
    if not text:
        return "", False
    if text.startswith("在此填入") or text.lower() in {token.lower() for token in PLACEHOLDER_TOKENS}:
        return "", True
    return text, False


def _expand(value: Any) -> Any:
    """递归展开 ``${VAR}`` / ``${VAR:-默认值}`` 占位符。"""
    if isinstance(value, Mapping):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, str):
        match = _PLACEHOLDER.match(value.strip())
        if match:
            resolved = _env_first(match.group(1))
            return resolved or (match.group(2) or "").strip()
    return value


def _as_path(root: Path, value: Any, default: str) -> Path:
    """把配置里的路径解析为绝对路径（相对路径以项目根为基准）。"""
    raw = _as_text(value, default)
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _deep_merge(base: MutableMapping[str, Any], extra: Mapping[str, Any]) -> None:
    """字典深度合并：嵌套 dict 递归合并，其余类型直接覆盖。"""
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), MutableMapping):
            _deep_merge(base[key], value)  # type: ignore[arg-type]
        else:
            base[key] = dict(value) if isinstance(value, Mapping) else value


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path} 顶层必须是键值映射，实际为 {type(data).__name__}")
    return {str(key): _expand(value) for key, value in data.items()}


def load_config_dir(config_dir: Path) -> dict[str, Any]:
    """按文件名排序合并目录下所有 ``*.yaml`` / ``*.yml``（后者覆盖前者）。"""
    merged: dict[str, Any] = {}
    if not config_dir.is_dir():
        log.warning("配置目录不存在：%s", config_dir)
        return merged
    for path in sorted(list(config_dir.glob("*.yaml")) + list(config_dir.glob("*.yml"))):
        _deep_merge(merged, _read_yaml(path))
    return merged


def _upward(base: Path) -> Iterable[Path]:
    path = base if base.is_dir() else base.parent
    yield path
    yield from path.parents


def find_project_root(start: str | Path | None = None) -> Path:
    """定位项目根：``FOUNDRY_ROOT`` > 从 start/cwd/包位置向上找 ``pyproject.toml``。"""
    env_root = _env_first(ENV_ROOT)
    if env_root:
        root = Path(env_root).expanduser()
        if not root.is_dir():
            raise ConfigError(f"{ENV_ROOT} 指向的目录不存在：{env_root}")
        return root.resolve()

    anchors: list[Path] = []
    if start is not None:
        anchors.append(Path(start).expanduser().resolve())
    anchors.append(Path.cwd())
    anchors.append(Path(__file__).resolve().parent)
    for anchor in anchors:
        for candidate in _upward(anchor):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    # src 布局兜底：<root>/src/foundry/config.py -> <root>
    fallback = Path(__file__).resolve().parents[2]
    return fallback if (fallback / "config").is_dir() else Path.cwd()


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 ``.env`` 解析（仅在没有 python-dotenv 时使用）。"""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        if text.startswith("export "):
            text = text[len("export "):].lstrip()
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_env_file(root: Path) -> Path | None:
    """读取项目根 ``.env``；已存在的环境变量优先（不被文件覆盖）。"""
    env_path = root / ".env"
    if not env_path.is_file():
        return None
    if _load_dotenv is not None:
        _load_dotenv(env_path, override=False)
    else:  # pragma: no cover
        for key, value in _parse_env_file(env_path).items():
            os.environ.setdefault(key, value)
    log.debug("已加载 %s", env_path)
    return env_path


# ---------------------------------------------------------------- 配置分段
@dataclass(frozen=True)
class LLMSettings:
    """OpenAI 兼容客户端配置（百炼/DashScope）。"""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    provider: str = "dashscope"
    temperature: float = 0.4
    timeout: int = 120
    max_retries: int = 3
    api_key: str = field(default="", repr=False)
    api_key_placeholder: bool = False

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "LLMSettings":
        api_key, key_is_placeholder = _clean_api_key(_env_first(ENV_API_KEY) or _as_text(cfg.get("api_key")))
        return cls(
            base_url=_env_first(ENV_BASE_URL) or _as_text(cfg.get("base_url"), DEFAULT_BASE_URL),
            model=_env_first(ENV_MODEL) or _as_text(cfg.get("model"), DEFAULT_MODEL),
            provider=_as_text(cfg.get("provider"), "dashscope"),
            temperature=_as_float(cfg.get("temperature"), 0.4),
            timeout=_as_int(cfg.get("timeout"), 120),
            max_retries=_as_int(cfg.get("max_retries"), 3),
            api_key=api_key,
            api_key_placeholder=key_is_placeholder,
        )

    def require_api_key(self) -> str:
        """需要调用 LLM 时取 Key；没有则给出可操作的报错。"""
        if not self.api_key:
            if self.api_key_placeholder:
                raise ConfigError(
                    f"{ENV_API_KEY} 仍是示例占位值。请把项目根 .env 里的“在此填入你的百炼Key”"
                    f"换成真实百炼 Key，或 export {ENV_API_KEY}=真实Key。"
                )
            raise ConfigError(
                f"未找到 API Key。请在项目根 .env 写入 {ENV_API_KEY}=你的百炼Key"
                f"（参考 .env.example），或先 export {ENV_API_KEY}。"
            )
        return self.api_key

    def masked_key(self) -> str:
        if self.api_key:
            return f"已设置（{len(self.api_key)} 字符）"
        if self.api_key_placeholder:
            return "未设置（.env 里仍是示例占位值，需替换成真实 Key）"
        return "未设置"


@dataclass(frozen=True)
class TTSSettings:
    """edge-tts 默认口音与语速。"""

    voice: str = DEFAULT_TTS_VOICE
    rate: str = DEFAULT_TTS_RATE
    voices: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "TTSSettings":
        presets = cfg.get("voices")
        presets = dict(presets) if isinstance(presets, Mapping) else {}
        return cls(
            voice=_as_text(cfg.get("voice"), DEFAULT_TTS_VOICE),
            rate=_as_text(cfg.get("rate"), DEFAULT_TTS_RATE),
            voices=presets,
        )

    def resolve_voice(self, name: str | None) -> str:
        """接受口音别名（如 ``uk_female``）、完整 voice 名或 None，返回可用 voice。"""
        candidate = _as_text(name)
        if not candidate:
            return self.voice
        return self.voices.get(candidate, candidate)


@dataclass(frozen=True)
class FetchSettings:
    """抓取相关默认值（来自 sources.yaml 的 defaults 与 settings.yaml 的 fetch）。"""

    timeout: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    max_age_hours: int = 24
    max_attempts: int = 3
    backoff: float = 2.0
    limit_per_source: int | None = None

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "FetchSettings":
        limit = cfg.get("limit_per_source")
        limit_per_source = None if limit in (None, "") else _as_int(limit, 0) or None
        return cls(
            timeout=_as_int(cfg.get("timeout"), 20),
            user_agent=_as_text(cfg.get("user_agent"), DEFAULT_USER_AGENT),
            max_age_hours=_as_int(cfg.get("max_age_hours"), 24),
            max_attempts=_as_int(cfg.get("max_attempts"), 3),
            backoff=_as_float(cfg.get("backoff"), 2.0),
            limit_per_source=limit_per_source,
        )


@dataclass(frozen=True)
class NormalizeSettings:
    """清洗与聚类阈值（供 normalizer 使用）。"""

    max_age_hours: int = 24
    min_english_ratio: float = 0.6
    similarity: float = 0.55
    max_articles_per_story: int = 8

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], *, default_max_age: int = 24) -> "NormalizeSettings":
        return cls(
            max_age_hours=_as_int(cfg.get("max_age_hours"), default_max_age),
            min_english_ratio=_as_float(cfg.get("min_english_ratio"), 0.6),
            similarity=_as_float(cfg.get("similarity"), 0.55),
            max_articles_per_story=_as_int(cfg.get("max_articles_per_story"), 8),
        )


@dataclass(frozen=True)
class OutputDirs:
    """产物目录（全部相对项目根）。"""

    root: Path
    packs: Path
    posts: Path
    audio: Path
    raw: Path
    web: Path
    last_run: Path

    @classmethod
    def create(cls, root: Path, output_value: Any, web_value: Any) -> "OutputDirs":
        output_root = _as_path(root, output_value, "output")
        web_dir = _as_path(root, web_value, "web")
        return cls(
            root=output_root,
            packs=output_root / "packs",
            posts=output_root / "posts",
            audio=output_root / "audio",
            raw=output_root / "raw",
            web=web_dir,
            last_run=output_root / "last_run.json",
        )

    def ensure(self) -> None:
        """创建需要的目录（幂等）。"""
        for path in (self.root, self.packs, self.posts, self.audio, self.raw, self.web):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    """项目总配置。"""

    root: Path
    config_dir: Path
    sources: Path
    prompts: Path
    env_file: Path | None
    llm: LLMSettings
    tts: TTSSettings
    fetch: FetchSettings
    normalize: NormalizeSettings
    output: OutputDirs
    source_list: tuple[Mapping[str, Any], ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # ---- 源清单 ----
    def all_sources(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.source_list]

    def enabled_sources(self) -> list[dict[str, Any]]:
        """只返回 ``enabled: true`` 的源，条目已补齐 defaults 里的超时/UA 等。"""
        return [entry for entry in self.all_sources() if entry.get("enabled", True)]

    # ---- 路径 ----
    def rel(self, path: str | Path) -> str:
        """相对项目根的展示路径（用于日志）。"""
        target = Path(path)
        try:
            return str(target.resolve().relative_to(self.root))
        except ValueError:
            return str(target)

    # ---- 摘要 ----
    def summary(self) -> dict[str, Any]:
        """脱敏摘要，可安全写入日志或 output/last_run.json。"""
        return {
            "root": self.rel(self.root),
            "config_dir": self.rel(self.config_dir),
            "sources_file": self.rel(self.sources),
            "env_file": self.rel(self.env_file) if self.env_file else None,
            "sources_total": len(self.source_list),
            "sources_enabled": len(self.enabled_sources()),
            "llm_base_url": self.llm.base_url,
            "llm_model": self.llm.model,
            "llm_api_key": self.llm.masked_key(),
            "tts_voice": self.tts.voice,
            "tts_rate": self.tts.rate,
            "output_dir": self.rel(self.output.root),
        }


def _normalize_source(entry: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """把单条源配置补齐默认字段（源里写的值优先）。"""
    merged = {**defaults, **dict(entry)}
    merged["id"] = _as_text(merged.get("id")) or _as_text(merged.get("url"))
    merged["name"] = _as_text(merged.get("name"), merged["id"])
    merged["category"] = _as_text(merged.get("category"), "news")
    merged["type"] = _as_text(merged.get("type"), "rss")
    merged["url"] = _as_text(merged.get("url"))
    merged["enabled"] = bool(merged.get("enabled", True))
    return merged


def _section(cfg: Mapping[str, Any], name: str) -> dict[str, Any]:
    """取一个配置段：缺失给空字典，写成非字典直接报错（便于发现缩进/拼写问题）。"""
    value = cfg.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置段 {name}: 必须是键值映射，实际为 {type(value).__name__}")
    return dict(value)


def _build_sources(cfg: Mapping[str, Any], sources_file: Path, fetch: FetchSettings) -> tuple[Mapping[str, Any], ...]:
    """解析源清单并补齐默认字段；没有清单时降级为空并告警。"""
    raw_sources = cfg.get("sources")
    if raw_sources is None:
        if sources_file.is_file():
            raise ConfigError(f"{sources_file} 缺少 sources: 列表")
        log.warning("未找到源清单：%s", sources_file)
        return ()
    if not isinstance(raw_sources, list):
        raise ConfigError(f"{sources_file} 的 sources: 必须是列表")
    defaults = {
        "timeout": fetch.timeout,
        "user_agent": fetch.user_agent,
        "max_age_hours": fetch.max_age_hours,
    }
    entries = [_normalize_source(item, defaults) for item in raw_sources if isinstance(item, Mapping)]
    missing_url = [item["id"] for item in entries if not item["url"]]
    if missing_url:
        raise ConfigError(f"以下源缺少 url：{', '.join(missing_url)}")
    return tuple(entries)


def load_settings(root: str | Path | None = None) -> Settings:
    """加载全部配置，返回带属性的 :class:`Settings`。

    Args:
        root: 显式指定项目根；默认自动定位（便于测试用 tmp_path）。
    """
    project_root = find_project_root(root)
    env_file = load_env_file(project_root)
    config_dir = project_root / "config"
    cfg = load_config_dir(config_dir)
    if not cfg:
        log.warning("未读到任何配置：%s 为空，将只使用内置默认值", config_dir)

    paths = _section(cfg, "paths")
    fetch = FetchSettings.from_config({**_section(cfg, "defaults"), **_section(cfg, "fetch")})
    normalize = NormalizeSettings.from_config(_section(cfg, "normalize"), default_max_age=fetch.max_age_hours)
    sources_file = _as_path(project_root, paths.get("sources"), "config/sources.yaml")

    settings = Settings(
        root=project_root,
        config_dir=config_dir,
        sources=sources_file,
        prompts=_as_path(project_root, paths.get("prompts"), "prompts"),
        env_file=env_file,
        llm=LLMSettings.from_config(_section(cfg, "llm")),
        tts=TTSSettings.from_config(_section(cfg, "tts")),
        fetch=fetch,
        normalize=normalize,
        output=OutputDirs.create(project_root, paths.get("output"), paths.get("web")),
        source_list=_build_sources(cfg, sources_file, fetch),
        raw=cfg,
    )
    log.debug("配置已加载：%s", settings.summary())
    return settings


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    print(f"EnglishFoundry 配置（{settings.rel(settings.root)}）")
    for key, value in settings.summary().items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

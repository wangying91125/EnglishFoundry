"""EnglishFoundry · 全球实时英文内容工厂。

流水线：fetch → normalize → master → roles → platforms → tts → site。

顶层只按需暴露配置与数据模型（延迟导入），这样 `import foundry` 不必加载
yaml/openai 等第三方库，也让 `python -m foundry.<模块>` 干净可用。
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.1.0"

_LAZY: dict[str, str] = {
    "ConfigError": "foundry.config",
    "Settings": "foundry.config",
    "load_settings": "foundry.config",
    "Article": "foundry.models",
    "Story": "foundry.models",
    "ContentPack": "foundry.models",
}

__all__ = ["__version__", *sorted(_LAZY)]


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'foundry' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))

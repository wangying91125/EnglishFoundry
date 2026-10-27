"""foundry.site 测试：全程离线，用 ``tests/fixtures/sample_pack.json`` 当母版，不联网、不调 LLM、不配音。

覆盖：``build_site`` 的文件清单与首页（今日精选 / 角色与类别筛选 / 客户端搜索索引）、
故事详情页的九个板块与音频播放器 + 下载入口、角色页（优先用 output/posts 的现成 Markdown、
认领平台文案）、about 页的流水线与信源、静态资产复制、空产物 / 缺模板 / 坏 JSON 的降级，
以及 Markdown 渲染的转义安全与命令行入口。断言都用 HTML 里的关键字，改模板时一眼看出破了哪块。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from foundry import site
from foundry.config import ENV_ROOT, load_settings
from foundry.models import ContentPack
from foundry.site import (
    AudioClip,
    SiteError,
    SiteStory,
    build_site,
    load_audio_clips,
    markdown_to_html,
    story_from_pack,
)
from foundry.site import _main as site_main
from foundry.tts import MANIFEST_NAME if False else None  # placeholder replaced below

"""端到端编排：fetch → normalize → master → roles → platforms → (tts) → site。

分工与约定：
    * 本模块只负责“串联 + 落盘 + 记账”，不重新实现任何业务逻辑：抓取交给
      :mod:`foundry.fetcher`，聚合交给 :mod:`foundry.normalizer`，母版交给
      :mod:`foundry.master`，角色文案交给 :mod:`foundry.roles`，平台文案交给
      :mod:`foundry.platforms`，配音交给 :mod:`foundry.tts`，静态站点交给
      :mod:`foundry.site`。想单独调任何一段，直接用那些模块，不必经过这里。
    * 每个 Story 的产物形状固定为：
      ``output/packs/<story_id>.json``（母版）、
      ``output/posts/<story_id>/<role>.md``（角色 Markdown）、
      ``output/posts/<story_id>/<role>/<platform>.md``（平台文案）、
      ``output/audio/<story_id>-listening.mp3``（听力配音），最后重建 ``web/``。
      平台文案放在 ``<story_id>/`` 下面，是因为 :func:`foundry.site.discover_posts`
      靠目录名认领归属：多篇故事共用 ``posts/<role>/<platform>.md`` 会互相覆盖，
      站点只能看到最后写进去的那一篇。
    * 失败隔离：一段、一篇、一个角色出错都只往 ``failures`` 记一笔，其余照常产出；
      运行摘要（处理源数 / 故事数 / 角色数 / 失败项……）写 ``output/last_run.json``，
      键的顺序即站点 About 页的展示顺序（:func:`foundry.site.build_site` 只取前 10 项）。
    * ``dry_run=True`` 全程离线：fetch / master 改用包内素材
      ``src/foundry/fixtures/dry_run.json``，tts 只写占位 mp3，绝不出网、绝不调用付费 LLM；
      roles / platforms / site 本身是纯函数，照常真跑，所以 ``--dry-run`` 也能产出完整站点。

用法::

    from foundry.pipeline import run

    summary = run(limit=2, dry_run=True)              # 离线冒烟，不花钱
    summary = run(roles="business,kids", with_tts=False)  # 真实日更（需要 DASHSCOPE_API_KEY）
    print(summary["stories"], summary["roles"], summary["failures"])

日常请用命令行：``python scripts/run_daily.py --dry-run --limit 2``
"""
from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from foundry.config import Settings, load_settings
from foundry.fetcher import fetch_all
from foundry.master import generate_pack
from foundry.models import Article, ContentPack, Story, iso_or_none
from foundry.normalizer import normalize
from foundry.platforms import load_platforms, render_for_platform
from foundry.roles import load_roles, render_for_role, topic_match
from foundry.site import build_site
from foundry.tts import (
    SynthResult,
    batch_synth,
    resolve_rate,
    resolve_voice,
    safe_name,
    target_path,
    write_manifest,
)

__all__ = [
    "DEFAULT_MIN_TOPIC_MATCH",
    "FIXTURE_NAME",
    "LISTENING_SUFFIX",
    "PLACEHOLDER_MP3",
    "STAGES",
    "SUMMARY_NAME",
    "PipelineError",
    "dry_run_fixture",
    "fixture_articles",
    "listening_text",
    "pack_for_story",
    "run",
    "write_last_run",
]

log = logging.getLogger("foundry.pipeline")

#: 流水线段名（顺序 = 执行顺序，也是 last_run.json 里 stages 的键）
STAGES: tuple[str, ...] = ("fetch", "normalize", "master", "roles", "platforms", "tts", "site")
#: dry-run 素材文件名（放在本包 fixtures/ 下，随包一起安装）
FIXTURE_NAME = "dry_run.json"
#: 运行摘要文件名
SUMMARY_NAME = "last_run.json"
#: 每篇故事的听力音频 key 后缀（站点靠 story_id 前缀认领音频）
LISTENING_SUFFIX = "listening"
#: 题材契合度门槛：0.0 = 所有配置角色都出稿；>0 时按 topic_match 筛角色
DEFAULT_MIN_TOPIC_MATCH = 0.0
#: dry-run 占位音频：真 mp3 帧头 + 静音帧，形状合法但不出网（About 页与测试都认它是音频文件）
PLACEHOLDER_MP3 = b"ID3\x03\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 420


class PipelineError(RuntimeError):
    """编排本身的问题（参数不认识、配置读不出、素材缺失），发生在产出之前。"""


# ---------------------------------------------------------------- 小工具
def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_keys(value: Any) -> list[str]:
    """把 ``"a,b"`` / ``["a", "b"]`` / ``None`` 归一成去重保序的 key 列表（空 → 空列表）。"""
    if value is None:
        return []
    items: list[Any]
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, Iterable):
        items = list(value)
    else:
        items = [value]
    keys: list[str] = []
    for item in items:
        key = _as_text(item).lower().replace("-", "_").replace(" ", "_")
        key = key.strip("_")
        if key and key not in keys:
            keys.append(key)
    return keys


def _select_keys(value: Any, table: Mapping[str, Any], label: str) -> list[str]:
    """校验并排序用户点名的 key；没点名就给全部（按配置顺序）。"""
    requested = _split_keys(value)
    if not requested:
        return list(table)
    unknown = [key for key in requested if key not in table]
    if unknown:
        raise PipelineError(f"未知{label}：{', '.join(unknown)}（可用：{', '.join(table)}）")
    return [key for key in table if key in requested]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- dry-run 素材
def dry_run_fixture(path: str | Path | None = None) -> dict[str, Any]:
    """读取离线素材（示例文章 + 母版模板）：``dry_run`` 的 fetch/master 两段都靠它。"""
    target = Path(path) if path is not None else Path(__file__).resolve().parent / "fixtures" / FIXTURE_NAME
    if not target.is_file():
        raise PipelineError(f"缺少 dry-run 素材：{target}（正常安装应随包提供 fixtures/{FIXTURE_NAME}）")
    payload = _read_json(target)
    if not isinstance(payload, dict) or not payload.get("articles") or not payload.get("packs"):
        raise PipelineError(f"dry-run 素材格式不对：{target} 需要同时含 articles 与 packs")
    return payload


def fixture_articles(fixture: Mapping[str, Any], *, now: datetime | None = None) -> list[Article]:
    """素材里的示例文章 → ``List[Article]``。

    ``published`` 写作相对分钟数（``age_hours``，可以是小数），每次运行按 ``now`` 折算，
    所以永远能过 normalizer 的时效过滤，也不会把 1970 年的“新闻”送进站点。
    """
    moment = now or datetime.now(timezone.utc)
    articles: list[Article] = []
    for entry in fixture.get("articles") or ():
        if not isinstance(entry, Mapping):
            continue
        age = entry.get("age_hours")
        published = moment - timedelta(hours=float(age)) if age is not None else entry.get("published")
        articles.append(
            Article(
                source=_as_text(entry.get("source")),
                category=_as_text(entry.get("category"), "news"),
                title=_as_text(entry.get("title")),
                url=_as_text(entry.get("url")),
                published=published,
                content=_as_text(entry.get("content")),
                tags=list(entry.get("tags") or ()),
            )
        )
    if not articles:
        raise PipelineError("dry-run 素材里没有可用的 articles")
    return articles


def pack_for_story(
    story: Story,
    fixture: Mapping[str, Any],
    *,
    index: int = 0,
    path: str | Path | None = None,
) -> ContentPack:
    """素材里的母版模板 → 这篇 Story 的 ``ContentPack``（不调 LLM）。

    模板靠 ``event`` 与示例文章对应（文章里也写 ``event``），对不上时退回按序号取，
    保证 ``limit`` 大于模板数时依然有产出。
    """
    templates = [item for item in (fixture.get("packs") or ()) if isinstance(item, Mapping)]
    if not templates:
        raise PipelineError("dry-run 素材里没有可用的 packs")
    events = {
        _as_text(entry.get("url")): _as_text(entry.get("event"))
        for entry in (fixture.get("articles") or ())
        if isinstance(entry, Mapping)
    }
    wanted = {events.get(_as_text(article.url)) for article in story.articles}
    wanted.discard(None)
    wanted.discard("")
    template = next((item for item in templates if _as_text(item.get("event")) in wanted), None)
    if template is None:
        template = templates[index % len(templates)]
        log.debug("Story %s 没匹配到 dry-run 模板，按序号用 %s", story.id, _as_text(template.get("event")))
    data = {key: copy.deepcopy(value) for key, value in template.items() if key not in {"event", "story_id"}}
    pack = ContentPack(story_id=story.id, data=data, path=Path(path) if path is not None else None)
    apply_story_meta(pack, story)
    return pack


def apply_story_meta(pack: ContentPack, story: Story) -> ContentPack:
    """把 Story 的标题/来源/时间/标签补进母版 JSON 的 ``meta``（LLM 只产出 9 个内容键）。

    母版本身不带这些字段，站点与平台文案会退回 B1 标题；补上之后 About 页、详情页和
    平台文案里的出处才是真实来源。dry-run 与真实日更走同一条路径。
    """
    pack.data["meta"] = {
        "title": story.title,
        "summary": story.summary,
        "url": story.url,
        "source": ", ".join(story.sources) or story.source,
        "category": story.category,
        "published": iso_or_none(story.published),
    }
    tags = list(dict.fromkeys(tag for article in story.articles for tag in article.tags))
    if tags:
        pack.data["tags"] = tags
    pack.data["story_id"] = pack.story_id
    return pack


def listening_text(pack: ContentPack) -> str:
    """这篇故事的听力稿：优先 ``listening_script``，退回 B1 正文，再退回任意一档正文。"""
    script = _as_text(pack.data.get("listening_script"))
    if script:
        return script
    levels = pack.data.get("levels")
    if isinstance(levels, Mapping):
        for level in ("B1", "A2", "B2", "A1", "C1", "C2"):
            entry = levels.get(level)
            if isinstance(entry, Mapping) and _as_text(entry.get("body")):
                return _as_text(entry.get("body"))
    return ""


# ---------------------------------------------------------------- 产出落盘
def _post_path(directory: Path, role_key: str, platform_key: str = "") -> Path:
    return directory / role_key / f"{platform_key}.md" if platform_key else directory / f"{role_key}.md"


def _write_post(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def _placeholder_synth(items: Sequence[Mapping[str, Any]], loaded: Settings) -> list[SynthResult]:
    """dry-run 配音：写占位 mp3，返回与 :func:`foundry.tts.batch_synth` 同形状的结果。"""
    voice = resolve_voice(None, loaded)
    rate = resolve_rate(None, loaded)
    results: list[SynthResult] = []
    for item in items:
        key = _as_text(item.get("key")) or "clip"
        target = target_path(loaded.output.audio, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PLACEHOLDER_MP3)
        results.append(
            SynthResult(key=key, voice=voice, rate=rate, path=target, ok=True, bytes=len(PLACEHOLDER_MP3))
        )
    log.info("dry-run 占位配音：%s 段（未联网，真实配音请去掉 --dry-run）", len(results))
    return results


# ---------------------------------------------------------------- 主流程
def run(
    limit: int | None = None,
    roles: Sequence[str] | str | None = None,
    platforms: Sequence[str] | str | None = None,
    with_tts: bool = True,
    dry_run: bool = False,
    *,
    settings: Settings | None = None,
    min_topic_match: float = DEFAULT_MIN_TOPIC_MATCH,
    write_summary: bool = True,
) -> dict[str, Any]:
    """跑一遍完整流水线，返回运行摘要（同时写 ``output/last_run.json``）。

    Args:
        limit: 最多处理几个 Story（``None`` 全量）。抓取仍按配置的每源条数走。
        roles: 只给这些角色出稿（逗号串或列表）；``None`` = ``config/roles.yaml`` 全部角色。
        platforms: 只出这些平台文案；``None`` = ``config/platforms.yaml`` 全部平台。
        with_tts: 是否配音（``False`` 时整段跳过，不写 ``output/audio``）。
        dry_run: 离线模式——fetch/master 用包内素材，tts 写占位 mp3，不出网也不调付费 LLM。
        settings: 复用已加载的配置；``None`` 时 :func:`foundry.config.load_settings`。
        min_topic_match: 题材契合度门槛（见 :func:`foundry.roles.topic_match`），
            默认 ``0.0`` 表示不筛；调高可以只给真正对口的角色出稿，省下产出。
        write_summary: 是否写 ``output/last_run.json``（单测里可以关掉）。

    Returns:
        摘要字典：前 10 个标量/列表项就是 About 页要显示的“处理源数 / 文章数 / 故事数 /
        角色数 / 平台数 / 文案数 / 音频数 / 失败项”，后面还有 ``packs`` ``site_files``
        ``stages`` ``outputs`` ``ok`` 等细节。

    Raises:
        PipelineError: 参数不认识（未知角色/平台）或配置、素材读不出来——这类问题
            在产出之前就发生，直接抛出，让命令行给非零退出码。
    """
    loaded = settings or load_settings()
    started = time.monotonic()
    clock = datetime.now(timezone.utc)
    cap = None if limit is None else max(0, _as_int(limit, 0))
    role_table = _load_table(load_roles, loaded, "角色")
    platform_table = _load_table(load_platforms, loaded, "平台")
    wanted_roles = _select_keys(roles, role_table, "角色")
    wanted_platforms = _select_keys(platforms, platform_table, "平台")
    loaded.output.ensure()
    mode = "dry-run" if dry_run else "live"
    log.info("EnglishFoundry 流水线开始（%s 模式，limit=%s，roles=%s，platforms=%s，tts=%s）",
             mode, cap if cap is not None else "-", len(wanted_roles), len(wanted_platforms), with_tts)

    failures: list[str] = []
    stages: dict[str, str] = {name: "skipped" for name in STAGES}

    def fail(stage: str, label: str, detail: Any) -> None:
        message = f"{stage}: {label}：{detail}" if label else f"{stage}: {detail}"
        failures.append(message)
        stages[stage] = "failed"
        log.warning("失败：%s", message)

    def done(stage: str) -> None:
        stages[stage] = stages[stage] if stages[stage] == "failed" else "ok"

    # ---- 1) fetch ----
    fixture: Mapping[str, Any] | None = None
    articles: list[Article] = []
    sources = 0
    if dry_run:
        fixture = dry_run_fixture()
        articles = fixture_articles(fixture, now=clock)
        sources = len({_as_text(article.source) for article in articles})
        log.info("dry-run 抓取：%s 个素材源 / %s 篇文章（未联网）", sources, len(articles))
    else:
        sources = len(loaded.enabled_sources())
        try:
            articles = list(fetch_all(settings=loaded))
        except Exception as error:  # noqa: BLE001 - 抓取整体失败也只记账，后面照走空流程
            fail("fetch", "抓取全部信源", error)
        if articles:
            log.info("抓取：%s 个源 / %s 篇文章", sources, len(articles))
        else:
            fail("fetch", "没有抓到任何文章", "请检查网络与 config/sources.yaml")
    done("fetch")

    # ---- 2) normalize ----
    stories: list[Story] = []
    try:
        stories = list(normalize(articles, loaded))
    except Exception as error:  # noqa: BLE001
        fail("normalize", "清洗聚合", error)
    if cap is not None:
        stories = stories[:cap]
    if articles and not stories and stages["normalize"] != "failed":
        fail("normalize", "清洗后没有可用 Story", "多半是全部超出 normalize.max_age_hours")
    _save_raw(loaded, articles, stories, fail, enabled=not dry_run)
    done("normalize")
    log.info("聚合：%s 个 Story（limit=%s）", len(stories), cap if cap is not None else "-")

    # ---- 3) master / 4) roles / 5) platforms ----
    packs: list[ContentPack] = []
    posts: list[Path] = []
    roles_used: list[str] = []
    platforms_used: list[str] = []
    audio_items: list[dict[str, Any]] = []
    for index, story in enumerate(stories):
        pack: ContentPack | None = None
        try:
            if dry_run and fixture is not None:
                pack = pack_for_story(story, fixture, index=index)
                pack.write(loaded.output.packs / f"{safe_name(story.id) or story.id}.json")
            else:
                pack = generate_pack(story, loaded, save=False)
                apply_story_meta(pack)
                pack.write(loaded.output.packs / f"{safe_name(pack.story_id) or pack.story_id}.json")
        except Exception as error:  # noqa: BLE001 - 一篇母版失败不影响下一篇
            fail("master", f"Story {story.id}", error)
            continue
        packs.append(pack)
        done("master")
        log.info("母版 %s -> %s", story.id, loaded.rel(pack.path) if pack.path else "(未落盘)")

        story_posts = loaded.output.posts / safe_name(story.id)
        for role_key in wanted_roles:
            if min_topic_match > 0:
                try:
                    score = topic_match(pack, role_key, role_table)
                except Exception as error:  # noqa: BLE001
                    fail("roles", f"{story.id} × {role_key}", error)
                    continue
                if score < min_topic_match:
                    log.debug("跳过角色 %s：题材契合度 %.2f < %.2f", role_key, score, min_topic_match)
                    continue
            try:
                markdown = render_for_role(pack, role_key, role_table)
                posts.append(_write_post(_post_path(story_posts, role_key), markdown))
            except Exception as error:  # noqa: BLE001 - 单个角色失败只少一份产出
                fail("roles", f"{story.id} × {role_key}", error)
                continue
            if role_key not in roles_used:
                roles_used.append(role_key)
            meta = _platform_meta(story, pack, role_key)
            for platform_key in wanted_platforms:
                try:
                    text = render_for_platform(markdown, meta, platform_key, platform_table)
                    posts.append(_write_post(_post_path(story_posts, role_key, platform_key), text))
                except Exception as error:  # noqa: BLE001
                    fail("platforms", f"{story.id} × {role_key} × {platform_key}", error)
                    continue
                if platform_key not in platforms_used:
                    platforms_used.append(platform_key)
        done("roles")
        done("platforms")
        text = listening_text(pack)
        if text:
            audio_items.append({"key": f"{safe_name(story.id) or story.id}-{LISTENING_SUFFIX}", "text": text})
    log.info("文案：%s 份 Markdown（%s 个角色 × %s 个平台），母版 %s 份",
             len(posts), len(roles_used), len(platforms_used), len(packs))

    # ---- 6) tts ----
    audio_results: list[SynthResult] = []
    if with_tts and audio_items:
        if dry_run:
            audio_results = _placeholder_synth(audio_items, loaded)
        else:
            try:
                audio_results = list(batch_synth(audio_items, settings=loaded))
            except Exception as error:  # noqa: BLE001 - 没装 edge-tts / 断网都不该带走整条流水线
                fail("tts", "批量配音", error)
        for result in audio_results:
            if not result.ok:
                fail("tts", result.key, result.error or "未产出音频")
        if audio_results:
            try:
                write_manifest(audio_results, settings=loaded)
            except Exception as error:  # noqa: BLE001
                fail("tts", "写 manifest", error)
        done("tts")
    elif with_tts:
        stages["tts"] = "skipped"
        log.info("配音：没有要合成的文本")
    audio_ok = sum(1 for result in audio_results if result.ok)

    summary: dict[str, Any] = {
        # —— 以下 10 项会显示在站点 About 页（site._last_run 只取前 10 个标量/列表）——
        "run_at": iso_or_none(clock),
        "mode": mode,
        "sources": sources,
        "articles": len(articles),
        "stories": len(stories),
        "roles": len(roles_used),
        "platforms": len(platforms_used),
        "posts": len(posts),
        "audio": audio_ok,
        "failures": failures,
        # —— 以下是排障细节，不进 About 页 ——
        "dry_run": dry_run,
        "with_tts": with_tts,
        "limit": cap,
        "packs": len(packs),
        "site_files": 0,
        "duration_seconds": round(time.monotonic() - started, 2),
        "stages": stages,
        "outputs": {
            "packs": loaded.rel(loaded.output.packs),
            "posts": loaded.rel(loaded.output.posts),
            "audio": loaded.rel(loaded.output.audio),
            "web": loaded.rel(loaded.output.web),
            "summary": loaded.rel(loaded.output.last_run),
        },
        "ok": bool(packs) and not failures,
    }

    # 摘要先落盘再建站点：About 页的 “Last pipeline run” 读的就是这个文件，
    # 顺序反过来时首次运行永远显示空摘要（site_files 不在展示的那 10 项里，不受影响）。
    flush(summary, loaded, write_summary)

    # ---- 7) site ----
    site_files: list[Path] = []
    try:
        site_files = list(build_site(loaded, limit=cap))
    except Exception as error:  # noqa: BLE001
        fail("site", "生成静态站点", error)
    else:
        done("site")
        log.info("站点：%s 个文件（%s）", len(site_files), loaded.rel(loaded.output.web))

    summary["site_files"] = len(site_files)
    summary["duration_seconds"] = round(time.monotonic() - started, 2)
    summary["ok"] = bool(packs) and not failures
    flush(summary, loaded, write_summary)
    log.info(
        "流水线结束：%s 源 / %s 故事 / %s 角色 / %s 失败项（%s 秒）",
        sources,
        len(stories),
        len(roles_used),
        len(failures),
        summary["duration_seconds"],
    )
    return summary

def flush(summary: dict[str, Any], settings: Settings, write_summary: bool = True) -> None:
    """按需把运行摘要写进 ``output/last_run.json``（写盘失败也只记一笔，不抛出）。"""
    if not write_summary:
        return
    try:
        write_last_run(summary, settings)
    except OSError as error:  # pragma: no cover - 磁盘问题没什么可挽救的
        summary["failures"].append(f"summary: 写 {settings.output.last_run}：{error}")
        summary["ok"] = False
        log.warning("失败：summary 写盘：%s", error)


def _load_table(loader: Any, loaded: Settings, label: str) -> dict[str, Any]:
    """读角色 / 平台配置表：读不出就直接报错，别等到一半才失败。"""
    try:
        return loader(settings=loaded)
    except Exception as error:  # noqa: BLE001 - 配置问题属于“跑不起来”，交给调用方转成退出码
        raise PipelineError(f"{label}配置读不出来：{error}") from error


def _platform_meta(story: Story, pack: ContentPack, role_key: str) -> dict[str, Any]:
    """喂给 :func:`foundry.platforms.render_for_platform` 的元信息（母版里的标题优先，Story 兜底）。"""
    meta = pack.data.get("meta") if isinstance(pack.data.get("meta"), Mapping) else {}
    levels = pack.data.get("levels") if isinstance(pack.data.get("levels"), Mapping) else {}
    level = next((key for key in ("B1", "A2", "B2", "A1", "C1", "C2") if key in levels), "B1")
    title = _as_text(meta.get("title")) or story.title
    return {
        "title": title,
        "url": _as_text(meta.get("url")) or story.url,
        "source": _as_text(meta.get("source")) or story.source,
        "date": _as_text(meta.get("published")) or iso_or_none(story.published) or "",
        "level": level,
        "story_id": pack.story_id,
        "role": role_key,
        "tags": list(pack.data.get("tags") or ()),
        "summary": _as_text(meta.get("summary")) or story.summary,
    }


def _save_raw(
    loaded: Settings,
    articles: Sequence[Article],
    stories: Sequence[Story],
    fail: Callable[[str, str, Any], None],
    *,
    enabled: bool,
) -> None:
    """把抓取与聚合结果留在 ``output/raw/``（排障用，也方便单独重跑 master）。

    dry-run 不写：素材是包里的，没必要盖掉用户上一次真实抓取的原始数据。
    """
    if not enabled:
        return
    try:
        _write_json(loaded.output.raw / "articles.json", [article.to_dict() for article in articles])
        _write_json(loaded.output.raw / "stories.json", [story.to_dict() for story in stories])
    except OSError as error:
        fail("normalize", "写 output/raw", error)


def write_last_run(summary: Mapping[str, Any], settings: Settings | None = None) -> Path:
    """把运行摘要写成 ``output/last_run.json``（键顺序即 About 页展示顺序）。"""
    loaded = settings or load_settings()
    target = _write_json(loaded.output.last_run, dict(summary))
    log.info("运行摘要 -> %s", loaded.rel(target))
    return target


def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - 便捷入口，验收走 scripts/run_daily.py
    """``python -m foundry.pipeline --dry-run --limit 2``（和 scripts/run_daily.py 等价）。"""
    import argparse

    parser = argparse.ArgumentParser(description="跑一遍 EnglishFoundry 端到端流水线")
    parser.add_argument("--limit", type=int, default=None, help="最多处理几个 Story")
    parser.add_argument("--roles", default=None, help="逗号分隔的角色 key（默认全部）")
    parser.add_argument("--platforms", default=None, help="逗号分隔的平台 key（默认全部）")
    parser.add_argument("--no-tts", action="store_true", help="跳过配音")
    parser.add_argument("--dry-run", action="store_true", help="离线：用包内素材，不出网、不调 LLM")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = run(
        limit=args.limit,
        roles=args.roles,
        platforms=args.platforms,
        with_tts=not args.no_tts,
        dry_run=args.dry_run,
    )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

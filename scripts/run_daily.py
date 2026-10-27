#!/usr/bin/env python3
"""每日跑一遍 EnglishFoundry 端到端流水线（fetch → normalize → master → roles → platforms → tts → site）。

    python scripts/run_daily.py                      # 真实日更：抓取 + 调 LLM + 配音 + 重建站点
    python scripts/run_daily.py --dry-run --limit 2   # 离线冒烟：用包内素材，不出网、不花钱
    python scripts/run_daily.py --roles business,kids --platforms weibo,x --no-tts

退出码（给 cron / GitHub Actions 用）：
    0  全部成功，没有任何失败项
    1  跑完了但有失败项（某个源、某篇母版、某段配音……，明细见 output/last_run.json）
    2  没跑起来：参数不认识、配置读不出、素材缺失
  130  被 Ctrl-C 打断
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130


def _bootstrap() -> None:
    """没 `pip install -e .` 时也能直接跑：把 ``src`` 挂进 sys.path。"""
    try:
        import foundry  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(ROOT / "src"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_daily.py",
        description="EnglishFoundry 每日流水线：抓取 → 聚合 → 母版 → 角色 → 平台 → 配音 → 静态站点",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="最多处理几个 Story（默认全量）")
    parser.add_argument("--roles", default=None, metavar="KEYS", help="逗号分隔的角色 key（默认 config/roles.yaml 全部）")
    parser.add_argument(
        "--platforms", default=None, metavar="KEYS", help="逗号分隔的平台 key（默认 config/platforms.yaml 全部）"
    )
    parser.add_argument("--min-topic-match", type=float, default=0.0, metavar="0-1", help="只给题材契合度达标的角色出稿")
    parser.add_argument("--no-tts", action="store_true", help="跳过配音（不产出 output/audio）")
    parser.add_argument("--dry-run", action="store_true", help="离线：用包内 fixture，不调用付费 LLM、不出网")
    parser.add_argument("--json", action="store_true", dest="as_json", help="把运行摘要以 JSON 打印到 stdout")
    parser.add_argument("-q", "--quiet", action="store_true", help="只打印摘要，不打印流水线日志")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 debug 日志")
    return parser


def configure_logging(quiet: bool, verbose: bool) -> None:
    if quiet:
        logging.getLogger("foundry").setLevel(logging.ERROR)
        return
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def format_summary(summary: dict[str, Any]) -> str:
    """人话版摘要（``--json`` 之外永远打印这个）。"""
    stage_bits = ", ".join(f"{name}:{summary['stages'].get(name, '-')}" for name in summary["stages"])
    lines = [
        f"{'dry-run 冒烟' if summary['dry_run'] else '真实日更'}结束，耗时 {summary['duration_seconds']} 秒"
        f"（{'成功' if summary['ok'] else '有失败项'}）",
        f"  信源 {summary['sources']} 个 → 文章 {summary['articles']} 篇 → Story {summary['stories']} 个",
        f"  母版 {summary['packs']} 份 → 角色 {summary['roles']} 个 × 平台 {summary['platforms']} 个"
        f" → 文案 {summary['posts']} 份",
        f"  配音 {summary['audio']} 段 → 站点 {summary['site_files']} 个文件（{summary['outputs']['web']}）",
        f"  各段状态：{stage_bits}",
        f"  运行摘要：{summary['outputs']['summary']}",
    ]
    if summary["failures"]:
        lines.append(f"  失败 {len(summary['failures'])} 项：")
        lines.extend(f"    - {item}" for item in summary["failures"])
    else:
        lines.append("  失败 0 项")
    return "\n".join(lines)


def exit_code(summary: dict[str, Any]) -> int:
    """0 = 一切成功；1 = 有失败项或没产出任何母版。"""
    return EXIT_OK if summary.get("ok") else EXIT_PARTIAL


def main(argv: Sequence[str] | None = None) -> int:
    _bootstrap()
    from foundry.config import ConfigError
    from foundry.pipeline import PipelineError, run

    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.quiet, args.verbose)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 需要是正整数（不给上限就别传）")
    if not 0 <= args.min_topic_match <= 1:
        parser.error("--min-topic-match 需要在 0-1 之间")

    try:
        summary = run(
            limit=args.limit,
            roles=args.roles,
            platforms=args.platforms,
            with_tts=not args.no_tts,
            dry_run=args.dry_run,
            min_topic_match=args.min_topic_match,
        )
    except (PipelineError, ConfigError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - 交互 Ctrl-C
        print("已中断（Ctrl-C）", file=sys.stderr)
        return EXIT_INTERRUPTED

    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_summary(summary))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())

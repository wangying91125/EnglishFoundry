任务：实现端到端编排 src/foundry/pipeline.py 与 scripts/run_daily.py，并补全测试。
要求：
- pipeline.py：run(limit=None, roles=None, platforms=None, with_tts=True, dry_run=False) 串联 fetch→normalize→master→roles→platforms→(tts)→site；dry_run 用本地 fixture 不调用付费 LLM。
- 每个 Story 产出：output/packs/*.json、output/posts/<role>/<platform>.md、output/audio/*.mp3、更新 web/。
- 记录运行摘要（处理源数、故事数、角色数、失败项）到 output/last_run.json。
- scripts/run_daily.py：命令行参数 --limit、--roles、--no-tts、--dry-run，退出码反映成功/失败。
- 补全 tests/，确保 `pytest -q` 全部通过（离线、用 fixture/mock）。
验收：`pytest -q` 全绿；`python scripts/run_daily.py --dry-run --limit 2` 成功并生成 web/index.html。

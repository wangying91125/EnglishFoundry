# EnglishFoundry

全球实时英文内容工厂：抓取全球英文源 → 聚合成事件 → LLM 生成分级母版 → 多角色 / 多平台文案 → 免费配音 → 静态站点。

## 快速开始

要求 Python 3.10+。

```bash
pip install -e ".[dev]"      # 以 src 布局安装 foundry 包 + pytest
cp .env.example .env         # 填入你的百炼 Key（.env 已被 .gitignore 忽略）
python -c "from foundry.config import load_settings; print(load_settings().llm.base_url)"
pytest                       # 离线单测
```

拿不到 PyPI（离线/受限网络）时，包本身仍可用：`pip install -e . --no-deps --no-build-isolation`，
或临时 `PYTHONPATH=src python -m foundry.config`；依赖需另行准备。

## 配置分层

优先级：环境变量 > 项目根 `.env` > `config/*.yaml` > 代码内置默认值。

- `config/sources.yaml`：全球英文源清单 + 抓取默认值（超时、UA、时效）。
- `config/settings.yaml`：路径、`llm`、`tts`、`fetch`、`normalize` 配置。
- `config/roles.yaml`：12 个角色（难度偏好 / 取用模块 / 语气 / 词数上限 / 专属旋钮），改这里就能调风格，不用动代码。
- `config/platforms.yaml`：6 个平台的呈现规格（内容形态 / 标题规则 / 长度硬上限 / 标签池 / 语言），改文案风格与预算也只用改这里。
- 识别的环境变量：`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`FOUNDRY_MODEL`；可用 `FOUNDRY_ROOT` 显式指定项目根。
- 照抄 `.env.example` 而没填的占位 Key 视为“未配置”（不会伪装成可用 Key），报错会提示替换成真实 Key。
- yaml 中可写 `${VAR}` 或 `${VAR:-默认值}` 占位符；仓库里不保存任何密钥明文，缺 Key 时 `settings.llm.require_api_key()` 会给出可操作报错。
- 所有路径相对项目根解析；`settings.output.ensure()` 负责创建产物目录。
- 排障：`python -m foundry.config` 打印脱敏后的生效配置。

## 抓取

```bash
python -c "from foundry.fetcher import fetch_all; print(len(fetch_all(limit_per_source=2)))"  # 需要能联网
python -m foundry.fetcher --limit 3 --save      # 打印每个源的篇数，并写 output/raw/articles.json
```

- feed 自带正文（`content:encoded` / Atom `<content>`）就直接用；只有摘要时才用 requests + BeautifulSoup 抓原文页面，抓不到就退回摘要，不丢条目。
- 每个源独立 try/except 并记录日志：单源超时、403、坏 XML 只影响它自己；出网统一带自定义 User-Agent 与超时。
- 重试用 tenacity 指数退避，最多 `fetch.max_attempts` 次（默认 3）；4xx 直接失败不空耗，5xx/429/超时才重试。
- 源里可写的可选字段：`timeout`、`user_agent`、`fulltext`（`auto`/`always`/`never`）、`min_fulltext_chars`、`max_content_chars`；每源条数上限取 `fetch.limit_per_source`（`fetch_all(limit_per_source=...)` 可覆盖，非正数为不限制）。
- `published` 统一成 timezone-aware UTC，`id` 是 URL 的稳定哈希；时效过滤与聚类留给 normalizer。
- feedparser / tenacity / lxml 缺失时自动降级（标准库 XML 解析、内置退避、`html.parser`），所以 `pytest` 全部离线可跑；抓取不用任何付费或需登录的 API，也不依赖 jina.ai。

## 清洗与聚合

```bash
python -m foundry.normalizer --hours 24 --save   # 读 output/raw/articles.json，打印并写 stories.json
python -c "from foundry.config import load_settings; from foundry.normalizer import normalize; print(len(normalize([], load_settings())))"
```

- `normalize(articles, settings)` = 清洗 → 英文过滤 → 时效过滤 → 去重 → 同事件聚类，返回 `List[Story]`；`prefilter` / `cluster` 也可单独用。
- `clean_html` 去标签、解实体、统一空白；装了 BeautifulSoup 就用它，没装退化为正则（结果一致），所以离线照跑。
- 英文判断只看字母里 ASCII 字母的占比（`normalize.min_english_ratio`，默认 0.6）；时效为 `normalize.max_age_hours`（默认 24h，`0` 关闭），没带时间的条目保留。
- 去重两个键：规范化 URL（小写主机、去 fragment/`utm_*` 等跟踪参数/默认端口）与归一化标题（小写、去音标、去标点）；命中即丢，保留最新一篇。
- 聚类用标题关键词（去停用词 + 轻度还原词形）做 single-link Jaccard，`normalize.similarity`（默认 0.55）以上并入同一 Story，单事件最多 `normalize.max_articles_per_story` 篇。
- `Story.title` 取最新一篇、`Story.primary` / `Story.published` 是最早的一手报道，`Story.summary` 取 primary 正文前几句；产出按“来源数多 → 时间新”排列。

## 母版内容包

```bash
python -m foundry.master --dry-run    # 只渲染提示词并打印目标路径，不调 LLM、不花钱
python -m foundry.master --limit 1    # 读 output/raw/stories.json -> output/packs/<story_id>.json
```

- `generate_pack(story, settings, modules=...)` 调 OpenAI 兼容 `chat.completions`（`base_url` / `api_key` / `model` / `temperature` / `timeout` / `max_retries` 全部来自 `settings.llm`），返回 `ContentPack` 并写 `output/packs/<story_id>.json`。
- 母版九个键：`levels`（A1-C2 各改写 title/body）、`bilingual`（基于 B1 的逐句 `{en, zh}`）、`vocabulary`（12 个 `word/pos/zh/en_def/example`）、`grammar`（3 条长难句）、`background_zh`（中文背景 ≤150 字）、`listening_script`（B1 口语化听力稿）、`speaking`（3 个讨论问题 + 1 个角色扮演）、`writing`（1 道题目 + 120-200 词范文）、`quiz`（5 题 `q/choices/answer/explain`）。
- `modules` 只生成需要的键（支持 `vocab`、`listening` 等别名与 `levels,quiz` 逗号串，未知名字直接报错）；本次没请求的键不写入，模型漏给的键补空值并告警。
- 提示词在 `prompts/master.md`，占位符写作 `{{MODULES}}` / `{{TITLE}}` / `{{BODY}}`（不用 `str.format`，正文里全是花括号）；文件缺失时退化为内置精简版提示词。
- 请求带 `response_format={"type": "json_object"}`；重试走 tenacity 指数退避（最多 `llm.max_retries` 次，与 fetcher 同口径：5xx/429/超时/解析失败才重试，4xx 立即失败）。
- JSON 解析容错：剥 ```json 围栏、扫描第一个配平对象（忽略前后缀废话）、容忍尾逗号与字符串内裸换行；解析不出来当作可重试失败。
- `validate_pack(data)` 返回数量/字段/长度的“待改进”清单（只告警，不篡改模型产出）；`clean_pack` 只做无损整形：`levels` 键名归一为 A1-C2、`quiz` 的 `answer` 统一成大写字母（接受 `"(B)"`、`"B. 选项"`、选项原文、序号）。
- 没装 openai SDK 时自动退回 requests 实现同名接口（`client.chat.completions.create`）；测试注入假 client，因此 `pytest` 全程离线、不产生任何调用费用。

## 角色渲染

```bash
python -m foundry.roles --list                                  # 看 12 个角色的配置摘要
python -m foundry.roles --pack output/packs/demo.json --role kids
python -m foundry.roles --pack output/packs/demo.json --role kids,developer --out output/posts
```

- `load_roles()` 读 `config/roles.yaml`：每个角色七个字段 `key` / `name` / `levels` / `modules` / `tone` / `word_limit` / `extra`；`modules` 复用母版别名表（`vocab` → `vocabulary`），写错键名/难度直接报错。
- `render_for_role(pack, role_key)` 返回一段 Markdown：`# 标题` → 角色说明块（语气 / 重点 / 题材命中）→ 正文 → 各学习板块 → `## 📝 小练习` → `<details>` 答案折叠区 → 收尾任务；不联网、不调 LLM。
- 难度按 `levels` 顺序取母版里存在的第一个；一档都没有就按 CEFR 距离就近回退并在开头标明（宁可让用户读得动，也不硬上超纲内容）。
- 母版里缺的键（`--modules levels,vocab` 只生成一部分时会发生）直接跳过对应板块，不报错、不硬凑。
- `word_limit` 是**整篇**词数硬上限（英文按词、中文按字、数字按串，`count_words()`）；超预算就按 `extra.trim_order` 声明的顺序把板块逐档降级（丢例句 → 丢英文释义 → 减条数 → 练习压成一行题干 → 整段去掉），正文最后动刀、答案折叠区留到最后，压完再“回填”一次，产出贴着预算而不是砍过头。
- 角色侧重写在 `extra` 里：`body_words` 正文词数、`vocab_count`/`quiz_count`/`grammar_count` 等条数、`keywords` 题材词（命中的词在词汇表里加 ⭐，`topic_match()` 给 0-1 契合度供 pipeline 排序）、`cta` 收尾任务、`tts_voice`/`tts_rate` 配音提示（本模块只透传）。
- 例如 `kids` 只允许 A1/A2 且词汇铺量到 12 个、`developer` 锁 B2/C1 并命中 data/server、`trade` 保留逐句对照好照抄给客户、`academic` 上 C1/C2 重摘要写作。
- 渲染是纯函数：同一母版 + 同一角色永远得到同一份 Markdown；写盘由 `pipeline` / `platforms` 负责。

## 平台文案

```bash
python -m foundry.platforms --list                                  # 6 个平台的呈现规格
python -m foundry.platforms --pack output/packs/demo.json --role business --platform weibo,x --out output/posts/business
```

- `load_platforms()` 读 `config/platforms.yaml`：约定 8 字段 `key` / `name` / `format` / `title_rule` / `char_limit` / `emoji` / `hashtags` / `notes`，另有 3 项扩展 `language`（`zh` = 中文框架 + 英文学习内容，`en` = 全英文）、`unit`（`chars` 字符数 / `words` 词数）与 `char_min`（长文下限，只作目标不硬凑）。
- `render_for_platform(role_markdown, meta, platform_key)` 吃角色 Markdown + `meta`（`title` / `url` / `source` / `date` 等，可空），吐一条该平台文案；`Platform.fits(文案)` 恒为真，验收就是各平台自己的 `char_limit`。`render_all_platforms()` 一次出 6 条。
- 长度保证分两条路：短平台（小红书 / 口播 / 微博 / X）用「预算内逐行拼装配」——话题标签先行预留永不被截、放不下先砍词表再降正文；长平台（公众号 / LinkedIn）按 detail 逐档降级（去例句 → 减词数 → 砍章节 → 截正文），最后才按行/词边界硬截。
- 形态速览：小红书 ≈600 字钩子标题 + 分点词卡 + 话题标签；公众号小标题分层深度长文（≤3000 字）；口播 30-60 秒逐字稿（前 3 秒钩子在第一行，≤320 字）；微博 ≤140 字含 `#话题#` 与短链；X 全英文单帖 ≤280 字符 + 1-2 个 hashtag；LinkedIn 全英文专业帖 300-600 词、结尾 follow CTA。
- 中文框架句是固定模板，英文学习内容逐字取自角色 Markdown（头条 / 正文 / 词卡 / 长难句 / 提问）：离线渲染，不翻译、不调 LLM；全英文平台不会漏出中文注释。配置里新增平台没有专属 builder 也能渲染：按 `language` / `unit` / `char_limit` 走兜底 builder。

## 配音

```bash
python -m foundry.tts --list-voices                                                 # 口音别名与默认语速
python -m foundry.tts --text "Hello from EnglishFoundry" --out output/audio/hello.mp3   # 需要能联网
python -m foundry.tts --file output/posts/business/xiaohongshu.md --voice uk_female --rate=-10% --manifest
```

- `synth_one(text, out_path, voice, rate)` 是异步接口（`await` / `asyncio.run`），`batch_synth(items, out_dir)` 是同步包装：一个事件循环 + 有界并发跑完整批。
- 引擎是 edge-tts（微软 Edge 在线 TTS）：**免费、不要 Key，但要能联网**；没装 `edge-tts` 时导入模块和 `--list-voices` 照常用，真正配音才报 `TtsUnavailable` 并提示 `pip install "edge-tts>=6.1.0"`。
- 默认口音 `en-US-AriaNeural`、默认语速 `+0%`（都来自 `config/settings.yaml` 的 `tts`）；`voice` 写别名 `us_female` / `us_male` / `uk_female` / `au_female` 或任意 edge-tts 口音名，`rate` 写 `"+5%"` / `"-10%"`，与 `roles.yaml` 透传的 `tts_voice` / `tts_rate` 提示同一套。
- `items` 形状随意：`{"文件名": "文本"}`、`[("key", "文本", voice, rate), ...]`、`[SynthItem, ...]`、或带 `key`/`text`/`tts_voice`/`tts_rate` 的映射与对象；输出固定是 `out_dir/{key}.mp3`（key 会被安全化，不能逃逸目录）。
- **失败隔离**：单条失败只记日志并把错误留在该条的 `SynthResult` 里，其他条目照跑，返回顺序与输入一致；写盘是原子的（先 `.mp3.part` 再改名），失败不会留下半个文件，`overwrite=False` / `--skip-existing` 支持增量重跑。
- 免费服务偶有抽风：单条默认重试 3 次（退避 1.5s×n）、90s 超时，`attempts` / `backoff` / `timeout` 可覆盖；`workers` 控制并发（1 为串行）。
- 角色/平台产物是 Markdown，默认先 `speech_text()` 清成口播稿（去标题符、强调符、表格线、链接 URL、`<details>`、emoji 与 `·｜` 分隔符，`C#` / `snake_case` 不误伤）；要逐字播报用 `--keep-markup` / `strip_markup=False`。
- 验收：联网环境跑 `FOUNDRY_TTS_LIVE=1 pytest tests/test_tts.py -k live`（真出一段 mp3 并校验字节）；其余用例全程离线，用 `monkeypatch` 把 `edge_tts.Communicate` 换成假的流式实现。

## 静态站点

```bash
python -m foundry.site                                            # 读 output/ 产物渲染到 web/，并打印文件清单
python -m foundry.site --packs tests/fixtures/sample_pack.json    # 用 fixture 母版出一个 demo 站
python -m foundry.site --limit 2 --no-audio                       # 冒烟：只渲染两篇、不复制音频
```

- `build_site(settings)` 是纯离线渲染器：读 `output/packs/*.json`（母版）、`output/posts/**/*.md`（角色 Markdown 与平台文案）、`output/audio/manifest.json` + `*.mp3`（配音）、`output/last_run.json`（运行摘要）与 `config/roles.yaml` / `config/platforms.yaml`，不联网、不调 LLM、不产新内容，返回生成（含复制）文件的清单 `List[Path]`。
- 页面：`web/index.html`（今日精选 + 按角色/类别筛选 + 客户端搜索）、`web/story/<story_id>.html`（A1–C2 档位切换、双语精读、词汇表、长难句、背景、听力稿、口语、写作、练习判分）、`web/role/<role_key>.html`（12 个角色各一页）、`web/about.html`（流水线、角色/平台表、信源、上次运行摘要）；模板在 `web/templates/`，样式与脚本在 `web/templates/static/`（构建时复制成 `web/static/`）。
- **纯静态**：无后端、无构建工具、无 CDN、无外部字体；搜索与筛选是原生 JS 在浏览器里对卡片 `data-roles` / `data-category` / `data-search` 做的过滤，不 `fetch` 任何 JSON，所以双击 `file://` 打开 `web/index.html` 也能用。
- 设计：浅色主题 + 系统字体栈，移动端响应式（`auto-fit` 网格、粘性导航、可横向滚动的档位条）；英文内容为主，导航与小节标题带中文说明；每个板块缺内容就整段不渲染，不会出现空壳。
- 音频：详情页与首页都列出 `<audio controls>` 播放器与 `download` 下载入口，mp3 复制进 `web/audio/`，`web/` 整目录即可直接部署；倍速按钮（0.75×–1.5×）走 `playbackRate` 并记住偏好。
- 母版 JSON 只有 9 个内容键，标题/来源/类别/时间不保证有：能读就读（顶层键或 `meta` / `story` 段），读不到就用 B1 标题、`news` 类别与文件时间兜底；`output/posts` 里现成的角色 Markdown 优先认领（`posts/<role>.md`、`posts/<story_id>/<role>.md`、`posts/<role>/<platform>.md`），没有才现场调 `render_for_role()` 渲染。
- 角色标签来自 `topic_match()` 的题材契合度（命中关键词才标，全都没命中时按难度偏好回退），首页角色筛选与角色页内容用的是同一份归属关系；坏 JSON、缺配置、缺音频都只降级并告警，单篇失败不拖垮整站。
- 重建默认清理 `web/story` / `web/role` / `web/audio` 里上一版遗留的产物（`--no-prune` 保留），模板目录永不动。

## 目录结构

- `src/foundry/config.py`：`load_settings()` 返回带属性的配置对象（`llm` / `tts` / `fetch` / `normalize` / `output` / `enabled_sources()`）。
- `src/foundry/models.py`：`Article`（单篇原文）、`Story`（同事件聚合）、`ContentPack`（LLM 母版产物）。
- `src/foundry/fetcher.py`：`fetch_all(enabled_only=True, limit_per_source=None)` / `fetch_one(source_cfg)`，按源清单抓 RSS/Atom，返回 `List[Article]`。
- `src/foundry/normalizer.py`：`normalize(articles, settings)`（另有 `prefilter` / `cluster` / `clean_html`），清洗去重后把同事件聚成 `List[Story]`。
- `src/foundry/roles.py`：`load_roles()` / `render_for_role(pack, role_key)` / `render_all()` / `topic_match()`，12 个角色的差异化 Markdown。
- `src/foundry/platforms.py`：`load_platforms()` / `render_for_platform(role_markdown, meta, platform_key)` / `render_all_platforms()`，6 平台适配文案（不联网、不调 LLM 的纯函数）。
- `src/foundry/tts.py`：`synth_one(text, out_path, voice, rate)`（异步）/ `batch_synth(items, out_dir)`（同步包装）/ `speech_text()` / `write_manifest()`，edge-tts 免费配音。
- `src/foundry/site.py`：`build_site(settings)` / `collect_stories`（公开为 `story_from_pack` + `load_packs`）/ `load_audio_clips()` / `discover_posts()` / `markdown_to_html()`，把 `output/` 产物渲染成 `web/` 静态站点并返回文件清单。
- 后续模块：`pipeline`。
- `output/`：产物目录（`packs/` `posts/` `audio/` `raw/` `last_run.json`，`raw/` 下放抓取的 `articles.json` 与聚合的 `stories.json`）；`web/`：静态站点（生成物 `index.html` `about.html` `story/` `role/` `static/` `audio/` + 模板 `web/templates/`）。
- `prompts/master.md`：母版生成的 LLM 提示词；`prompts/build/`：分模块任务指令。
- `tests/`：pytest 用例（全部离线可跑，`tests/test_tts.py` 里 monkeypatch 掉 `edge_tts.Communicate`，`tests/fixtures/sample_pack.json` 是母版样例；`tests/test_platforms.py` 用同一份角色 Markdown 验收 6 个平台的长度与格式；`tests/test_site.py` 在临时项目根里跑 `build_site`，验收首页筛选/搜索索引、详情页九板块、音频下载、角色页认领与 Markdown 转义）。

## 云端保存与断点恢复

引导脚本会启动 `scripts/cloud-sync.sh`，每 30 秒将工作区未提交的文件保存并推送到当前分支；任务阶段和 Codex 模块状态写入 `output/.progress/`，模块完成后立即推送。`.env`、`.foundry-state/` 等密钥/本地运行状态仍由 `.gitignore` 排除。同步依赖已登录且有推送权限的 GitHub `origin`；断网期间保留本地修改，恢复联网后自动重试。重新创建 Codespace 时，请先 `git pull` 获取最新内容。

```bash
bash scripts/cloud-sync.sh status  # 查看同步状态
bash scripts/cloud-sync.sh start   # 启动或重启同步
bash bootstrap.sh build            # 从首个未完成模块继续，已完成模块会跳过
bash scripts/cloud-sync.sh stop   # 停止后台同步
```

Codespaces 仅在容器仍运行时才能继续原终端中的进程；若容器被停止或重建，进程内存、未写盘缓冲和任意外部程序的执行位置无法从 GitHub 原样恢复。EnglishFoundry 的模块任务会根据已推送检查点重新接续；其他程序需自行把进度写入文件或数据库，并提供恢复入口。

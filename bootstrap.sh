#!/usr/bin/env bash
# =============================================================================
# EnglishFoundry 一键引导脚本（云端版 · 不挑操作系统）
# 在 GitHub Codespaces 终端运行：  bash bootstrap.sh
# 分阶段可重跑： setup / build / test / deploy / all（默认 all）
# =============================================================================
set -uo pipefail

# ---- 颜色与输出 ----
if [[ -t 1 ]]; then
	C_B=$'\033[1m'; C_DIM=$'\033[2m'; C_G=$'\033[32m'; C_Y=$'\033[33m'
	C_R=$'\033[31m'; C_C=$'\033[36m'; C_BLUE=$'\033[34m'; C_0=$'\033[0m'
else
	C_B=""; C_DIM=""; C_G=""; C_Y=""; C_R=""; C_C=""; C_BLUE=""; C_0=""
fi
line(){ printf '%s\n' "------------------------------------------------------------"; }
info(){ printf '%s[信息]%s %s\n' "$C_C" "$C_0" "$1"; }
ok(){ printf '%s[成功]%s %s\n' "$C_G" "$C_0" "$1"; }
warn(){ printf '%s[注意]%s %s\n' "$C_Y" "$C_0" "$1"; }
err(){ printf '%s[出错]%s %s\n' "$C_R" "$C_0" "$1"; }
step(){
	if [[ "$CLOUD_SYNC_READY" == "1" ]]; then
		refresh_latest || exit 1
	fi
	printf '\n%s▸ %s%s\n' "$C_B$C_BLUE" "$1" "$C_0"
	if [[ -d "$APP_DIR/output/.progress" ]]; then
		printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "$APP_DIR/output/.progress/run.log"
	fi
}

# ---- 路径 ----
WORK_DIR="$(pwd)"
APP_DIR="$WORK_DIR"
STATE_DIR="$WORK_DIR/.foundry-state"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
CODEX_MODEL="${FOUNDRY_CODEX_MODEL:-qwen3.8-flash}"
RUNTIME_MODEL="${FOUNDRY_RUNTIME_MODEL:-qwen-plus}"
mkdir -p "$STATE_DIR"
PROGRESS_DIR="$APP_DIR/output/.progress/modules"
CLOUD_SYNC_READY=0
BOOTSTRAP_PATH="$APP_DIR/$(basename "${BASH_SOURCE[0]}")"
BOOTSTRAP_ARGS=("$@")
BOOTSTRAP_LOADED_HASH="$(git -C "$APP_DIR" hash-object "$BOOTSTRAP_PATH" 2>/dev/null || true)"

refresh_latest(){
	if ! bash "$APP_DIR/scripts/cloud-sync.sh" refresh; then
		err "无法确认云端最新版；已停止本阶段，工作区修改会保留，联网后请重试。"
		return 1
	fi
	local current_hash
	current_hash="$(git -C "$APP_DIR" hash-object "$BOOTSTRAP_PATH" 2>/dev/null || true)"
	if [[ -n "$BOOTSTRAP_LOADED_HASH" && "$current_hash" != "$BOOTSTRAP_LOADED_HASH" ]]; then
		info "bootstrap 已更新，重新启动当前命令以执行最新版"
		exec bash "$BOOTSTRAP_PATH" "${BOOTSTRAP_ARGS[@]}"
	fi
}

phase_cloud_sync(){
	step "云端自动保存与任务恢复"
	mkdir -p "$PROGRESS_DIR"
	if [[ -f "$APP_DIR/scripts/cloud-sync.sh" ]]; then
		bash "$APP_DIR/scripts/cloud-sync.sh" start || return 1
		refresh_latest || return 1
		CLOUD_SYNC_READY=1
	else
		err "缺少 scripts/cloud-sync.sh，无法启动云端自动保存"
		return 1
	fi
	local marker
	for marker in "$STATE_DIR"/mod_*.done; do
		[[ -f "$marker" ]] || continue
		cp -n "$marker" "$PROGRESS_DIR/$(basename "$marker")"
	done
	bash "$APP_DIR/scripts/cloud-sync.sh" sync-once || warn "首次云端检查点推送失败；本地修改保留，自动保存器会继续重试"
}

# =============================================================================
# 阶段 1：环境检查
# =============================================================================
phase_check_env(){
	step "第 1 步 · 检查云端环境"
	local miss=0
	for c in node npm python3 git; do
		if command -v "$c" >/dev/null 2>&1; then
			ok "已安装 $c"
		else
			err "缺少 $c"; miss=1
		fi
	done
	if ! command -v gh >/dev/null 2>&1; then
		warn "缺少 GitHub CLI（gh），尝试安装..."
		if command -v apt-get >/dev/null 2>&1; then
			sudo apt-get update -y >/dev/null 2>&1 && sudo apt-get install -y gh >/dev/null 2>&1 || true
		fi
		command -v gh >/dev/null 2>&1 && ok "gh 安装成功" || warn "gh 自动安装失败，稍后可手动安装"
	else
		ok "已安装 gh"
	fi
	if [[ "$miss" == "1" ]]; then
		err "基础工具缺失，请确认你是在 GitHub Codespaces 里运行，而不是自己电脑。"
		return 1
	fi
	# Node 版本需 >=18
	local nv; nv="$(node -v | sed 's/v//' | cut -d. -f1)"
	if [[ "${nv:-0}" -lt 18 ]]; then
		err "Node 版本过低（需 18+，当前 $(node -v)）。Codespaces 默认已满足。"; return 1
	fi
	touch "$STATE_DIR/env.ok"
}

# =============================================================================
# 阶段 2：收集 API Key（只问一次）
# =============================================================================
phase_collect_keys(){
	step "第 2 步 · 配置千问 API Key（只输入这一次）"
	local key=""
	if [[ -f "$STATE_DIR/keys.env" ]]; then
		# shellcheck disable=SC1090
		source "$STATE_DIR/keys.env"; ok "已读取上次保存的 Key"; return 0
	fi
	if [[ -n "${DASHSCOPE_API_KEY:-}" ]]; then
		key="$DASHSCOPE_API_KEY"; ok "检测到环境变量 DASHSCOPE_API_KEY"
	else
		printf '%s' "$C_DIM"
		echo "  如何获取：登录 阿里云百炼 → 右上角 API-KEY → 创建。"
		echo "  新用户有免费额度；qwen-plus 约 0.8 元/百万输入、2 元/百万输出。"
		printf '%s' "$C_0"
		printf '  请粘贴你的 DASHSCOPE_API_KEY（输入时不显示，回车确认）：'
		read -rs key; echo
	fi
	if [[ -z "$key" ]]; then err "Key 不能为空"; return 1; fi
	umask 077
	cat > "$STATE_DIR/keys.env" << EOF
export DASHSCOPE_API_KEY='$key'
export DASHSCOPE_BASE_URL='$DASHSCOPE_BASE_URL'
EOF
	chmod 600 "$STATE_DIR/keys.env"
	ok "Key 已安全保存（不会提交到 GitHub）"
}

# =============================================================================
# 阶段 3：安装 Codex 并写入千问配置
# =============================================================================
phase_install_codex(){
	step "第 3 步 · 安装 AI 程序员 Codex 并接通千问"
	mkdir -p "$CODEX_HOME_DIR"
	if ! command -v codex >/dev/null 2>&1; then
		info "安装 @openai/codex（约 10-30 秒）..."
		npm install -g @openai/codex >/dev/null 2>&1
	fi
	command -v codex >/dev/null 2>&1 && ok "Codex 已就绪：$(codex --version 2>&1)" || { err "Codex 安装失败"; return 1; }

	umask 077
	cat > "$CODEX_HOME_DIR/config.toml" << EOF
# EnglishFoundry - Codex 接千问（自动生成，请勿提交到仓库）
model = "$CODEX_MODEL"
model_provider = "dashscope"
approval_policy = "never"
sandbox_mode = "workspace-write"

[model_providers.dashscope]
name = "DashScope"
base_url = "$DASHSCOPE_BASE_URL"
wire_api = "responses"
env_key = "DASHSCOPE_API_KEY"
EOF
	ok "已写入 Codex 配置（模型 $CODEX_MODEL，Responses 协议，自动审批）"
	touch "$STATE_DIR/codex.ok"
}

# =============================================================================
# 阶段 4：生成项目脚手架与全球源清单
# =============================================================================
phase_scaffold(){
	step "第 4 步 · 生成项目骨架与全球源清单"
	mkdir -p "$APP_DIR"/{config,src/foundry,prompts,scripts,tests,web,output}
	mkdir -p "$APP_DIR/.github/workflows"

	cat > "$APP_DIR/.gitignore" << 'EOF'
.env
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
.foundry-state/
.DS_Store
EOF

	cat > "$APP_DIR/.env.example" << EOF
DASHSCOPE_API_KEY=在此填入你的百炼Key
DASHSCOPE_BASE_URL=$DASHSCOPE_BASE_URL
FOUNDRY_MODEL=$RUNTIME_MODEL
EOF

	# 全球源清单（已实测；Actions 海外 IP 全部可达）
	cat > "$APP_DIR/config/sources.yaml" << 'EOF'
# 全球原版英文输入源
defaults:
  timeout: 20
  max_age_hours: 24
  user_agent: "EnglishFoundry/1.0 (+https://github.com)"
sources:
  - {id: bbc_world,   name: "BBC World",        category: news,      type: rss, enabled: true, url: "https://feeds.bbci.co.uk/news/world/rss.xml"}
  - {id: npr_top,     name: "NPR News",         category: news,      type: rss, enabled: true, url: "https://feeds.npr.org/1001/rss.xml"}
  - {id: guardian,    name: "The Guardian",     category: news,      type: rss, enabled: true, url: "https://www.theguardian.com/world/rss"}
  - {id: nyt_home,    name: "The New York Times", category: news,    type: rss, enabled: true, url: "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"}
  - {id: aljazeera,   name: "Al Jazeera",       category: news,      type: rss, enabled: true, url: "https://www.aljazeera.com/com/x/rss/all.xml"}
  - {id: dw_all,      name: "Deutsche Welle",   category: news,      type: rss, enabled: true, url: "https://rss.dw.com/rdf/rss-en-all"}
  - {id: reddit_world,name: "Reddit r/worldnews", category: community, type: rss, enabled: true, url: "https://www.reddit.com/r/worldnews/.rss"}
  - {id: hn_front,    name: "Hacker News",      category: community, type: rss, enabled: true, url: "https://hnrss.org/frontpage"}
  - {id: techcrunch,  name: "TechCrunch",       category: tech,      type: rss, enabled: true, url: "https://techcrunch.com/feed/"}
  - {id: verge,       name: "The Verge",        category: tech,      type: rss, enabled: true, url: "https://www.theverge.com/rss/index.xml"}
  - {id: wired,       name: "Wired",            category: tech,      type: rss, enabled: true, url: "https://www.wired.com/feed/rss"}
  - {id: ars,         name: "Ars Technica",     category: tech,      type: rss, enabled: true, url: "https://feeds.arstechnica.com/arstechnica/index"}
  - {id: mittr,       name: "MIT Technology Review", category: tech,  type: rss, enabled: true, url: "https://www.technologyreview.com/feed/"}
  - {id: nasa,        name: "NASA",             category: science,   type: rss, enabled: true, url: "https://www.nasa.gov/feed/"}
  - {id: nature,      name: "Nature",           category: academic,  type: rss, enabled: true, url: "https://www.nature.com/nature.rss"}
  - {id: arxiv_cs,    name: "arXiv Computer Science", category: academic, type: rss, enabled: true, url: "https://export.arxiv.org/rss/cs"}
  - {id: ted,         name: "TED Talks",        category: video,     type: rss, enabled: true, url: "https://feeds.feedburner.com/TEDTalks_audio"}
EOF
	ok "项目骨架与 17 个全球源已生成"
	touch "$STATE_DIR/scaffold.ok"
}

# =============================================================================
# 阶段 5：写入给 Codex 的 9 个模块提示词
# =============================================================================
phase_write_prompts(){
	step "第 5 步 · 生成给 Codex 的模块指令"
	local PD="$APP_DIR/prompts/build"; mkdir -p "$PD"

	cat > "$PD/00_base.md" << 'EOF'
任务：在当前项目（englishfoundry，全球实时英文内容工厂）建立可运行的 Python 基础。
创建：
1) pyproject.toml：定义包名 foundry，src 布局（package-dir = {"" = "src"}），依赖：feedparser、requests、beautifulsoup4、lxml、pyyaml、openai、python-dotenv、tenacity、jinja2、edge-tts；开发依赖 pytest。
2) src/foundry/__init__.py。
3) src/foundry/config.py：加载 config/*.yaml；用 dotenv 读取项目根 .env；环境变量 DASHSCOPE_API_KEY、DASHSCOPE_BASE_URL、FOUNDRY_MODEL 覆盖配置；提供 load_settings() 返回带属性的对象（含 sources 路径、llm.base_url、llm.api_key、llm.model、tts 默认口音/语速、output 目录）。
4) src/foundry/models.py：用 @dataclass 定义 Article（id, source, category, title, url, published, content, tags:list）、Story（id, title, summary, url, source, category, published, articles:list）、ContentPack（story_id, data:dict, path）。
要求：纯标准库+上述依赖；所有路径相对项目根；不写死任何 Key。
验收：`pip install -e .` 成功；`python -c "from foundry.config import load_settings; print(load_settings().llm.base_url)"` 能打印 URL。
EOF

	cat > "$PD/01_fetcher.md" << 'EOF'
任务：实现 src/foundry/fetcher.py，按 config/sources.yaml 抓取全球英文 RSS/Atom。
要求：
- 用 feedparser 解析 RSS；对没有正文的条目，用 requests+BeautifulSoup 抓取 url 正文（超时、自定义 User-Agent，失败则仅用摘要）。
- 每个源独立 try/except 并记录日志，单源失败不影响其他源。
- tenacity 指数退避重试（最多 3 次）。
- 输出 List[Article]，Article 字段见 foundry.models；published 统一为 timezone-aware datetime；id 用 url 的稳定哈希。
- 提供 fetch_all(enabled_only=True, limit_per_source=None) 与 fetch_one(source_cfg)。
不要使用任何付费或需登录的 API；不要依赖 jina.ai。
验收：`python -c "from foundry.fetcher import fetch_all; a=fetch_all(limit_per_source=2); print(len(a))"` 在能联网的环境返回 >0 条目。
EOF

	cat > "$PD/02_normalizer.md" << 'EOF'
任务：实现 src/foundry/normalizer.py，把 Article 清洗、去重、聚合成 Story。
要求：
- clean_html：去标签、统一空白、规整。
- 只保留英文内容（简单判断：英文字符占比）。
- 按时间过滤（默认最近 24h，可配）。
- 去重：URL 规范化 + 标题归一化（小写、去标点）完全相同去重。
- 同事件聚类：从标题提取关键词（去掉停用词），用 Jaccard 或 token 集合相似度（阈值可配，默认 0.55）合并为 Story，Story.articles 包含各来源。
- 提供 normalize(articles, settings) -> List[Story]。
验收：对构造的示例（同一事件两篇、不相关一篇）能聚成 2 个 Story；为该函数写 tests/test_normalizer.py。
EOF

	cat > "$PD/03_master.md" << 'EOF'
任务：实现“母版内容包”生成：src/foundry/master.py 与 prompts/master.md。
要求：
- prompts/master.md：要求模型对一篇英文新闻，输出严格 JSON，键为：
	levels（A1,A2,B1,B2,C1,C2 各自改写 title 与 body）、bilingual（逐句 {en, zh} 数组，基于 B1 版本）、vocabulary（[{word,pos,zh,en_def,example}]，12 个）、grammar（[{sentence, point, explanation_zh}]，3 条长难句）、background_zh（中文背景，150 字内）、listening_script（B1 口语化文本）、speaking（3 个讨论问题 + 1 个角色扮演场景）、writing（1 篇范文题目 + 范文 120-200 词）、quiz（[{q, choices, answer, explain}]，5 题）。
- master.py：用 OpenAI 兼容客户端（base_url/api_key/model 来自 settings.llm），chat.completions，response_format 要求 JSON；tenacity 重试；可通过 modules 参数选择生成哪些键；把结果保存为 output/packs/<story_id>.json 并返回 ContentPack。
- JSON 解析要健壮（剥离 ```json 围栏、容错）。
验收：用 monkeypatch 假的 LLM 返回（读 tests/fixtures/sample_pack.json）写 tests/test_master.py，不依赖真实网络即可通过。
EOF

	cat > "$PD/04_roles.md" << 'EOF'
任务：实现 12 角色渲染：config/roles.yaml 与 src/foundry/roles.py。
在 roles.yaml 定义 12 个角色（key, name, levels（偏好难度，可多个）、modules（从 ContentPack 取用的键）、tone、word_limit、extra）：
k12、postgrad（考研）、cet（四六级）、ielts_toefl（雅思托福）、business（商务职场）、trade（外贸）、crossborder（跨境电商）、developer（程序员/技术）、academic（学术）、creator（内容创作者/出海）、immigration（移民留学/海外生活）、kids（少儿亲子）。
要求：
- roles.py：load_roles()；render_for_role(pack:ContentPack, role_key) -> markdown 字符串，按该角色的 levels/modules/tone/word_limit 从母版选取并组装，含标题、正文、词汇、练习、答案折叠区。
- 每个角色的侧重真实合理（如 kids 只用 A1/A2 并多词汇、developer 侧重 tech 类与 B2-C1、trade 侧重商务表达）。
验收：render_for_role 对 fixture ContentPack 与至少 3 个角色返回非空 markdown 且长度受 word_limit 约束；写 tests/test_roles.py。
EOF

	cat > "$PD/05_platforms.md" << 'EOF'
任务：实现 6 平台渲染：config/platforms.yaml 与 src/foundry/platforms.py。
在 platforms.yaml 定义（key, name, format, title_rule, char_limit, emoji, hashtags, notes）：
xiaohongshu（小红书，钩子标题+分点+emoji，约 600 字，带话题标签）、wechat_mp（公众号，深度长文，小标题）、shortvideo（短视频口播，30-60 秒口语逐字稿，前 3 秒钩子）、weibo（微博，140 字内+#话题#）、x（X/Twitter，英文，280 字符内，1-2 个 hashtag）、linkedin（LinkedIn，英文专业，300-600 词，结尾 CTA）。
要求：platforms.py：load_platforms()；render_for_platform(role_markdown, meta, platform_key) -> 适配文案；中文平台输出中文框架+英文学习内容，英文平台输出英文。
验收：对同一角色 markdown，6 个平台均返回符合各自 char_limit/格式的文案；写 tests/test_platforms.py。
EOF

	cat > "$PD/06_tts.md" << 'EOF'
任务：实现免费配音 src/foundry/tts.py（edge-tts，微软，免费、可联网）。
要求：
- 用 edge-tts 异步接口，把文本合成 mp3；可选 voice（默认 en-US-AriaNeural，另提供 en-US-GuyNeural、en-GB-SoniaNeural、en-AU-NatashaNeural）、rate（默认 +0%）。
- 提供 async synth_one(text, out_path, voice, rate) 与同步包装 batch_synth(items, out_dir)。
- 输出路径与失败隔离（单条失败记录并继续）。
验收：在能联网环境为一段英文生成非空 mp3；写 tests/test_tts.py，用 monkeypatch 替换 edge_tts.Communicate 使其离线可测。
EOF

	cat > "$PD/07_site.md" << 'EOF'
任务：实现静态站点生成 src/foundry/site.py 与 web/ 模板。
要求：
- site.py：读取 output 内容（packs、角色 markdown、平台文案、音频路径），用 Jinja2 渲染静态站点到 web/：index.html（今日精选、按角色/类别筛选、客户端搜索）、story 详情页、role 页、about.html。
- 模板内联/放在 web/templates 或直接在 site.py 中；纯静态、无后端、无外部框架依赖（可少量原生 CSS/JS）。
- 设计：浅色主题、现代简洁、移动端响应式、英文内容为主、中文导航说明；列出音频播放器与下载入口。
- 提供 build_site(settings) 并输出生成的文件清单。
验收：build_site 对 fixture 数据生成 web/index.html 且文件非空、含关键字；写 tests/test_site.py。
EOF

	cat > "$PD/08_pipeline.md" << 'EOF'
任务：实现端到端编排 src/foundry/pipeline.py 与 scripts/run_daily.py，并补全测试。
要求：
- pipeline.py：run(limit=None, roles=None, platforms=None, with_tts=True, dry_run=False) 串联 fetch→normalize→master→roles→platforms→(tts)→site；dry_run 用本地 fixture 不调用付费 LLM。
- 每个 Story 产出：output/packs/*.json、output/posts/<role>/<platform>.md、output/audio/*.mp3、更新 web/。
- 记录运行摘要（处理源数、故事数、角色数、失败项）到 output/last_run.json。
- scripts/run_daily.py：命令行参数 --limit、--roles、--no-tts、--dry-run，退出码反映成功/失败。
- 补全 tests/，确保 `pytest -q` 全部通过（离线、用 fixture/mock）。
验收：`pytest -q` 全绿；`python scripts/run_daily.py --dry-run --limit 2` 成功并生成 web/index.html。
EOF
	ok "9 个模块指令已生成"
	touch "$STATE_DIR/prompts.ok"
}

# ---- 调用 Codex 生成单个模块（自动重试 3 次）----
run_codex_module(){
	local prompt_file="$1"
	local name; name="$(basename "$prompt_file" .md)"
	local marker="$PROGRESS_DIR/mod_${name}.done"
	local status="$PROGRESS_DIR/mod_${name}.status"
	refresh_latest || return 1
	if [[ -f "$marker" ]]; then info "模块 $name 已完成，跳过"; return 0; fi
	mkdir -p "$PROGRESS_DIR"
	printf 'running %s\n' "$(date -u +%FT%TZ)" > "$status"
	bash "$APP_DIR/scripts/cloud-sync.sh" sync-once || warn "模块开始状态暂未同步；自动保存器会继续重试"
	# shellcheck disable=SC1090
	source "$STATE_DIR/keys.env"; export DASHSCOPE_API_KEY DASHSCOPE_BASE_URL
	info "Codex 正在生成模块 $name（可在下方实时看到它的动作）..."
	local tries=0 done_f=0
	while (( tries < 3 )); do
		refresh_latest || return 1
		if codex exec --skip-git-repo-check -C "$APP_DIR" "$(cat "$prompt_file")"; then
			done_f=1; break
		fi
		tries=$((tries+1)); warn "模块 $name 第 $tries 次未成功，3 秒后重试..."; sleep 3
	done
	if [[ "$done_f" != "1" ]]; then
		printf 'failed %s\n' "$(date -u +%FT%TZ)" > "$status"
		bash "$APP_DIR/scripts/cloud-sync.sh" sync-once || warn "失败状态暂未推送；本地进度仍已保留"
		err "模块 $name 三次失败。可手动修复：在终端运行 cd $APP_DIR && codex，粘贴 $prompt_file 内容。"
		return 1
	fi
	touch "$marker"
	printf 'done %s\n' "$(date -u +%FT%TZ)" > "$status"
	bash "$APP_DIR/scripts/cloud-sync.sh" sync-once || warn "模块已完成，但云端检查点推送失败；自动保存器会继续重试"
	ok "模块 $name 完成"
}

# =============================================================================
# 阶段 6：Codex 逐模块生成整套系统
# =============================================================================
phase_build(){
	step "第 6 步 · 让 Codex 自动生成整套系统（约 15-30 分钟，无需操作）"
	command -v codex >/dev/null 2>&1 || { err "Codex 未安装，请先运行 bash bootstrap.sh setup"; return 1; }
	local f
	for f in "$APP_DIR"/prompts/build/*.md; do
		[[ -e "$f" ]] || continue
		run_codex_module "$f" || return 1
	done
	ok "全部模块生成完毕"
}

# =============================================================================
# 阶段 7：安装并跑通离线测试
# =============================================================================
phase_test(){
	step "第 7 步 · 安装依赖并跑通全部测试"
	cd "$APP_DIR"
	info "安装本项目（pip install -e .）..."
	python3 -m pip install -q -e . 2>&1 | tail -2 || { err "依赖安装失败"; cd "$WORK_DIR"; return 1; }
	info "运行 pytest（离线、使用示例数据）..."
	if python3 -m pytest -q; then ok "全部测试通过"; else
		err "测试未通过。可让 Codex 修复：cd $APP_DIR && codex，输入：修复所有失败的测试并重跑"
		cd "$WORK_DIR"; return 1
	fi
	info "运行 dry-run 生成示例站点（不调用付费模型）..."
	python3 scripts/run_daily.py --dry-run --limit 2 >/dev/null 2>&1 \
		&& ok "示例站点已生成到 web/" \
		|| warn "dry-run 未完成（不影响部署，可稍后用 Codex 排查）"
	cd "$WORK_DIR"
}

# =============================================================================
# 阶段 8：写入 GitHub Actions 工作流
# =============================================================================
phase_workflows(){
	step "第 8 步 · 配置自动化工作流（定时生产 + 自动部署）"
	local WF="$APP_DIR/.github/workflows"; mkdir -p "$WF"

	cat > "$WF/daily.yml" << 'EOF'
name: Daily production
on:
	schedule:
		- cron: "17 */6 * * *"
	workflow_dispatch:
permissions:
	contents: write
jobs:
	produce:
		runs-on: ubuntu-latest
		steps:
			- uses: actions/checkout@v4
			- uses: actions/setup-python@v5
				with: {python-version: "3.12"}
			- name: Install
				run: python -m pip install -e .
			- name: Produce content
				env:
					DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}
				run: python scripts/run_daily.py
			- name: Commit results
				run: |
					git config user.name "englishfoundry-bot"
					git config user.email "englishfoundry-bot@users.noreply.github.com"
					git add output web
					git commit -m "chore: daily content update" || echo "nothing to commit"
					git push
EOF

	cat > "$WF/deploy.yml" << 'EOF'
name: Deploy site
on:
	push:
		branches: [main, master]
		paths: ["web/**"]
	workflow_dispatch:
permissions:
	contents: read
	pages: write
	id-token: write
concurrency:
	group: pages
	cancel-in-progress: true
jobs:
	deploy:
		runs-on: ubuntu-latest
		environment:
			name: github-pages
			url: ${{ steps.deployment.outputs.page_url }}
		steps:
			- uses: actions/checkout@v4
			- uses: actions/configure-pages@v5
			- uses: actions/upload-pages-artifact@v3
				with: {path: web}
			- id: deployment
				uses: actions/deploy-pages@v4
EOF
	ok "工作流已写入（每 6 小时生产一次，web 更新即部署）"
}

# =============================================================================
# 阶段 9：提交、推送、配置 Secret
# =============================================================================
phase_git(){
	step "第 9 步 · 提交并推送到 GitHub"
	cd "$APP_DIR"
	git config user.email "foundry@local" 2>/dev/null || true
	git config user.name "EnglishFoundry" 2>/dev/null || true
	git add -A
	git commit -m "feat: EnglishFoundry system" >/dev/null 2>&1 || true
	if ! git remote get-url origin >/dev/null 2>&1; then
		err "当前目录没有关联 GitHub 仓库。请确认你是在从仓库创建的 Codespaces 里运行。"
		cd "$WORK_DIR"; return 1
	fi
	info "推送代码..."
	git push -u origin HEAD 2>&1 | tail -3 || { err "推送失败"; cd "$WORK_DIR"; return 1; }
	# shellcheck disable=SC1090
	source "$STATE_DIR/keys.env"; export DASHSCOPE_API_KEY
	if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
		gh secret set DASHSCOPE_API_KEY --body "$DASHSCOPE_API_KEY" >/dev/null 2>&1 \
			&& ok "已把千问 Key 配置为仓库 Secret（供定时任务使用）" \
			|| warn "Secret 自动配置失败，可手动在仓库 Settings → Secrets → Actions 添加 DASHSCOPE_API_KEY"
	fi
	cd "$WORK_DIR"
}

# =============================================================================
# 阶段 10：开启 GitHub Pages 并获取网址
# =============================================================================
phase_deploy(){
	step "第 10 步 · 开启网站"
	cd "$APP_DIR"
	command -v gh >/dev/null 2>&1 || { warn "无 gh，部署步骤见手册（开启 GitHub Pages）"; cd "$WORK_DIR"; return 0; }
	local repo owner name url
	repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
	[[ -z "$repo" ]] && { warn "无法获取仓库信息，可手动在 Settings → Pages 选择 GitHub Actions 部署"; cd "$WORK_DIR"; return 0; }
	owner="$(gh api "repos/$repo" -q .owner.login 2>/dev/null)"
	name="$(gh api "repos/$repo" -q .name 2>/dev/null)"
	gh api "repos/$repo/pages" -X POST -f build_type=workflow >/dev/null 2>&1 || true
	gh workflow run deploy.yml >/dev/null 2>&1 || true
	url="https://${owner}.github.io/${name}/"
	echo "$url" > "$STATE_DIR/site.url"
	ok "网站地址：$url"
	info "首次部署约 1-2 分钟生效；之后每 6 小时自动更新"
	cd "$WORK_DIR"
}

phase_summary(){
	line
	printf '%s🎉 EnglishFoundry 已部署完成！%s\n' "$C_B$C_G" "$C_0"
	local url; url="$(cat "$STATE_DIR/site.url" 2>/dev/null || echo '（见上方网址）')"
	echo "  你的网站：$url"
	echo ""
	echo "  接下来："
	echo "  1) 打开网址确认内容；在 GitHub 仓库 Actions 页可看每次自动运行。"
	echo "  2) 改 config/roles.yaml、platforms.yaml 可调整各角色/平台风格。"
	echo "  3) 变现：开通会员、向英语博主/老师推每日成品包、洽谈白标。"
	echo "  4) 任何报错，把 Actions 日志或截图发我，我带你修。"
	line
}

phase_prepare_scaffold(){
	if [[ -s "$APP_DIR/prompts/build/00_base.md" ]]; then
		info "检测到已有云端脚手架与模块提示词，保留最新版文件"
		return 0
	fi
	phase_scaffold && phase_write_prompts
}

# =============================================================================
# 主调度
# =============================================================================
case "${1:-all}" in
	setup)  phase_cloud_sync && phase_check_env && phase_collect_keys && phase_install_codex && phase_prepare_scaffold ;;
	build)  phase_cloud_sync && phase_build ;;
	test)   phase_cloud_sync && phase_test ;;
	deploy) phase_cloud_sync && phase_workflows && phase_git && phase_deploy && phase_summary ;;
	all)
		phase_cloud_sync && phase_check_env && phase_collect_keys && phase_install_codex && phase_prepare_scaffold \
			&& phase_build && phase_test && phase_workflows && phase_git && phase_deploy && phase_summary
		;;
	*) echo "用法：bash bootstrap.sh [setup|build|test|deploy|all]"; exit 1 ;;
esac

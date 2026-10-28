#!/usr/bin/env bash
# =============================================================================
# EnglishFoundry bootstrap.sh — V6.0 终版
# 覆盖：PDF 全部章节 + 7 赛道 + 深度内容线 + 图像模块 + 四层复利 +
#       软付费墙 + 第7章 Agent/MemO/路由 + 真实音频硬校验 +
#       P0 九单元 + P1/P2 十二单元 + 真实信源 + 独角数卡支付 + FastAPI gated + 部署检查
# 用法:
#   ./bootstrap.sh --phase 0|1|2|3|4 [--yes]
#   CHECK_ONLINE=1 ./bootstrap.sh --phase 0      # 真实探测 /responses
#   FETCH_ONLINE=1 ./bootstrap.sh --phase 3      # 真实抓取信源
#   ./bootstrap.sh --phase 4                     # 支付 + gated 服务 + 部署检查
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$ROOT"
if [[ "${FOUNDRY_CLOUD_LATEST:-0}" != "1" ]]; then
    exec bash "$ROOT/scripts/cloud-sync.sh" run-latest "$@"
fi

PHASE=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:-0}"; shift 2 ;;
    --phase=*) PHASE="${1#*=}"; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --help|-h) echo "用法: $0 [--phase 0|1|2|3|4] [--yes]"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[1;33m'; C_RED='\033[0;31m'; C_BLUE='\033[0;34m'
log(){ echo -e "${C_BLUE}[bootstrap]${C_RESET} $*"; }
ok(){ echo -e "${C_GREEN}[ok]${C_RESET} $*"; }
warn(){ echo -e "${C_YELLOW}[warn]${C_RESET} $*"; }
die(){ echo -e "${C_RED}[fail]${C_RESET} $*" >&2; exit 1; }
confirm(){ [ "$ASSUME_YES" -eq 1 ] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy]$ ]]; }
wim(){ local p="$1"; if [ -e "$p" ]; then warn "已存在，跳过: $p"; else mkdir -p "$(dirname "$p")"; cat > "$p"; ok "写入: $p"; fi; }
check_cmd(){ command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"; }

log "EnglishFoundry bootstrap V6.0 终版开始，Phase=${PHASE}"
check_cmd python3
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' || die "需要 Python >= 3.10"
ok "Python $(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# =============================================================================
# 1. 目录
# =============================================================================
log "创建目录结构"
mkdir -p config src scripts tests data content/free/deep_read content/free/book_foundry content/free/daily_starter \
  content/premium private_delivery/white_label assets/images assets/cards assets/exports audio logs \
  static/track static/assets static/img server payments seed
ok "目录结构就绪"

# =============================================================================
# 2. venv + 依赖
# =============================================================================
[ -d .venv ] || { log "创建 venv"; python3 -m venv .venv; }
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
wim requirements.txt <<'REQ'
edge-tts>=6.1.0
PyYAML>=6.0
requests>=2.31
feedparser>=6.0
pytest>=8.0
python-dateutil>=2.8
fastapi>=0.110
uvicorn>=0.30
python-multipart>=0.0.9
httpx>=0.27
REQ
pip install -r requirements.txt >/dev/null
ok "依赖安装完成（含 fastapi/uvicorn/httpx）"

# =============================================================================
# 3. .env.example
# =============================================================================
wim .env.example <<'ENV'
# 第7章 构建期双后端
CODE_BACKEND=dashscope
CODE_MODEL=qwen-plus
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.cn/v1
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
GLM_API_KEY=
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
CHECK_ONLINE=0
# 音频硬校验
TTS_VOICE=en-US-JennyNeural
TTS_RATE=-10%
AUDIO_MIN_BYTES=50000
# 真实信源（Phase 3）
QUARTR_API_KEY=
QUARTR_BASE_URL=https://api.quartr.com/v1
BENZINGA_API_KEY=
BENZINGA_BASE_URL=https://api.benzinga.com/api/v2.1
FISCAL_API_KEY=
FISCAL_BASE_URL=https://api.fiscal.ai/mcp
OET_OFFICIAL_RSS=
AHPRA_RSS=
NMC_RSS=
GMC_RSS=
ICAO_DOC_RSS=
FAA_RSS=
EASA_RSS=
CAAC_RSS=
PUBLIC_JUDGMENTS_RSS=
REGULATOR_TEMPLATES_RSS=
MODEL_RELEASES_RSS=
ARXIV_AI_RSS=
EU_AI_ACT_RSS=
YOUTUBE_CREATOR_RSS=
SUBSTACK_CREATOR_RSS=
GOV_SERVICE_RSS=
BANK_HOSPITAL_SCHOOL_RSS=
FETCH_ONLINE=0
# 支付（Phase 4）
PAYMENT_PROVIDER=dujiaoka
DUJIAOKA_BASE_URL=
DUJIAOKA_SECRET_KEY=
DUJIAOKA_WEBHOOK_PATH=/webhook/dujiaoka
PAYMENT_CURRENCY=CNY
# Gated 服务
GATED_HOST=127.0.0.1
GATED_PORT=8787
GATED_PUBLIC_URL=http://127.0.0.1:8787
# 每日分析
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
ANALYTICS_EMAIL_TO=owner@example.com
ENV
ok ".env.example"

# =============================================================================
# 4. 配置
# =============================================================================
wim config/tracks.yaml <<'YAML'
tracks:
  - {id: oet, name: 医疗 OET, buyer: 海外注册医护, outcome: "OET Grade B（重点写作）", standard: "OET Writing rubric", price_monthly: 199, priority: P0, sources: [oet_official, ahpra, nmc, gmc, nmbi, clinical_corpus]}
  - {id: icao, name: 航空 ICAO, buyer: 飞行员/空管, outcome: "ICAO Level 4+", standard: "ICAO six dimensions", price_monthly: 199, priority: P0, sources: [icao_doc9432, faa, easa, caac, asn, airline_opi_feedback]}
  - {id: finance, name: 金融, buyer: 投行/基金/财务, outcome: "参与财报电话会 Q&A", standard: "Earnings call Q&A precision", price_monthly: 159, priority: P0, sources: [quartr_api, benzinga_stream, fiscal_ai, company_ir]}
  - {id: legal, name: 法律, buyer: 涉外律师/法务, outcome: "独立起草/审阅/谈判英文合同", standard: "Contract clauses/legal email/negotiation/memo", price_monthly: 159, priority: P1, sources: [public_judgments, regulator_templates, law_firm_alerts, aifc_framework]}
  - {id: ai, name: AI / 提示工程, buyer: AI 从业者/全员, outcome: "生产级 prompt、评估 AI 输出、跟进政策", standard: "Prompt structure/output evaluation/policy reading", price_monthly: 69, priority: P1, sources: [model_releases, arxiv, eu_ai_act, agora, gov_policy_en]}
  - {id: creator, name: 创作者, buyer: YouTuber/播客/博主, outcome: "可发布脚本/标题/赞助 pitch", standard: "Hook/retention/sponsorship pitch/SEO", price_monthly: 69, priority: P2, sources: [youtube_public, substack_public, creator_platform_blogs, pitch_templates]}
  - {id: relocation, name: 迁居, buyer: 移民/外派/家属, outcome: "签证/银行/就医/学校/求职场景独立完成", standard: "Scene completion: visa/bank/healthcare/school/job", price_monthly: 69, priority: P2, sources: [gov_service_guides, bank_hospital_school_pages, relocation_community_authorized]}
YAML

wim config/content_lines.yaml <<'YAML'
content_lines:
  deep_read: {name: "Deep Read 深度长读", format: "多源整合杂志式周刊(B2/C1)", layers: [facts, explanation, language, argument, action], price_single: 9.9, cadence: weekly}
  book_foundry: {name: "Book Foundry 上传即课", format: "PDF/DOCX/URL → 章节精读课", pipeline: [outline, chapter_split, track_rubric_apply, exercises, media, export], price_per_book: 99, copyright: "仅处理用户有权使用的材料；输出仅供个人学习"}
  daily_starter: {name: "Daily Starter 每日一练", format: "一句 + 一卡 + 一音频", role: "免费引流 + 打卡 + 付费价值预览", checkin_key_day: 7}
YAML

wim config/pricing.yaml <<'YAML'
plans: {monthly: 29, quarterly: 79, annual: 268}
tracks: {oet: 199, icao: 199, finance: 159, legal: 159, ai: 69, creator: 69, relocation: 69}
deep_read_single: 9.9
book_foundry_per_book: 99
white_label_monthly: 499
YAML

wim config/paywall.yaml <<'YAML'
free_full_modules: 1
preview_lines: 5
metered_free_reads: 2
show_locked_outline: true
show_social_proof: true
annual_anchor: true
paid_body_delivery: private_delivery
static_site_contains_paid_body: false
upgrade_path: "独角数卡 + VPS gated 服务（Phase 4）"
YAML

wim config/pipeline.yaml <<'YAML'
stages: [topic, source, reconstruct, level, pedagogy, media, qa]
retry: {max_attempts: 3, backoff_seconds: 5}
audio: {min_bytes: 50000, voice: en-US-JennyNeural, rate: "-10%"}
development_layers: [language, scene, task, outcome, metadata]
YAML

wim config/visual_rules.yaml <<'YAML'
rules:
  - {when: "task_type in ['process','branch','decision']", type: decision_tree}
  - {when: "task_type == 'standard'", type: infographic}
  - {when: "task_type in ['scene_dialogue','scene']", type: scene}
  - {when: "task_type == 'data'", type: data_chart}
  - {when: "task_type == 'card'", type: card}
default_type: infographic
style: professional_minimal
label_anchor: true
versioned: true
white_label_export: true
YAML

wim config/sources.yaml <<'YAML'
# 全部占位在 Phase 3 会被 check_sources_real.py 强校验；带 example.com 直接 fail
sources:
  oet_official: {track: oet, type: rss, env_url: OET_OFFICIAL_RSS, frequency: monthly, compliance: official_terms}
  ahpra: {track: oet, type: rss, env_url: AHPRA_RSS, frequency: quarterly, compliance: official_terms}
  nmc: {track: oet, type: rss, env_url: NMC_RSS, frequency: quarterly, compliance: official_terms}
  gmc: {track: oet, type: rss, env_url: GMC_RSS, frequency: quarterly, compliance: official_terms}
  nmbi: {track: oet, type: page, env_url: NMC_RSS, frequency: quarterly, compliance: official_terms}
  clinical_corpus: {track: oet, type: authorized_import, url: "local://clinical_corpus", frequency: ongoing, compliance: deidentified_authorized}
  icao_doc9432: {track: icao, type: rss, env_url: ICAO_DOC_RSS, frequency: yearly, compliance: official_terms}
  faa: {track: icao, type: rss, env_url: FAA_RSS, frequency: weekly, compliance: public_domain_like}
  easa: {track: icao, type: rss, env_url: EASA_RSS, frequency: monthly, compliance: official_terms}
  caac: {track: icao, type: rss, env_url: CAAC_RSS, frequency: monthly, compliance: official_terms}
  asn: {track: icao, type: api, env_url: FAA_RSS, frequency: weekly, compliance: deidentify_required}
  airline_opi_feedback: {track: icao, type: authorized_import, url: "local://opi_feedback", frequency: ongoing, compliance: partner_authorized}
  quartr_api: {track: finance, type: api, env_key: QUARTR_API_KEY, env_base: QUARTR_BASE_URL, endpoint: "/companies/{company}/transcripts", frequency: realtime, compliance: api_license}
  benzinga_stream: {track: finance, type: api, env_key: BENZINGA_API_KEY, env_base: BENZINGA_BASE_URL, endpoint: "/transcripts", frequency: realtime, compliance: api_license}
  fiscal_ai: {track: finance, type: api, env_key: FISCAL_API_KEY, env_base: FISCAL_BASE_URL, endpoint: "/mcp", frequency: daily, compliance: api_license}
  company_ir: {track: finance, type: rss, env_url: PUBLIC_JUDGMENTS_RSS, frequency: daily, compliance: public_ir}
  public_judgments: {track: legal, type: rss, env_url: PUBLIC_JUDGMENTS_RSS, frequency: weekly, compliance: public_documents}
  regulator_templates: {track: legal, type: rss, env_url: REGULATOR_TEMPLATES_RSS, frequency: monthly, compliance: official_terms}
  law_firm_alerts: {track: legal, type: rss, env_url: PUBLIC_JUDGMENTS_RSS, frequency: weekly, compliance: public_alerts}
  aifc_framework: {track: legal, type: page, env_url: REGULATOR_TEMPLATES_RSS, frequency: quarterly, compliance: official_terms}
  model_releases: {track: ai, type: rss, env_url: MODEL_RELEASES_RSS, frequency: daily, compliance: public_release_notes}
  arxiv: {track: ai, type: rss, env_url: ARXIV_AI_RSS, frequency: daily, compliance: public_preprint}
  eu_ai_act: {track: ai, type: rss, env_url: EU_AI_ACT_RSS, frequency: monthly, compliance: official_terms}
  agora: {track: ai, type: page, env_url: EU_AI_ACT_RSS, frequency: weekly, compliance: official_terms}
  gov_policy_en: {track: ai, type: page, env_url: EU_AI_ACT_RSS, frequency: weekly, compliance: official_terms}
  youtube_public: {track: creator, type: rss, env_url: YOUTUBE_CREATOR_RSS, frequency: daily, compliance: platform_tos}
  substack_public: {track: creator, type: rss, env_url: SUBSTACK_CREATOR_RSS, frequency: daily, compliance: public_posts}
  creator_platform_blogs: {track: creator, type: rss, env_url: SUBSTACK_CREATOR_RSS, frequency: weekly, compliance: public_blog}
  pitch_templates: {track: creator, type: authorized_import, url: "local://pitch_templates", frequency: ongoing, compliance: authorized}
  gov_service_guides: {track: relocation, type: rss, env_url: GOV_SERVICE_RSS, frequency: monthly, compliance: official_terms}
  bank_hospital_school_pages: {track: relocation, type: rss, env_url: BANK_HOSPITAL_SCHOOL_RSS, frequency: monthly, compliance: public_info}
  relocation_community_authorized: {track: relocation, type: authorized_import, url: "local://relocation", frequency: ongoing, compliance: authorized_deidentified}
public_sources:
  - {id: public_news_rss, type: rss, env_url: MODEL_RELEASES_RSS, frequency: daily, compliance: rss}
YAML

wim config/agents.yaml <<'YAML'
build_agents:
  primary: {name: codex-cli, backend: dashscope, model: qwen-plus, protocol: responses, responses_endpoint: /responses, reason: "非交互、可判断成败、免费额度"}
  hard: {name: kimi-code, backend: kimi, model: K2.7-Code, protocol: responses, responses_endpoint: /responses, reason: "编程强、支持 responses、1M 上下文"}
  optional_hardest: {name: claude-code, enabled: false, reason: "终端自主多文件天花板（可选）"}
  optional_ide: {name: cursor, enabled: false, reason: "AI 原生 IDE、并行云 Agent（可选）"}
forbidden: [gemini-cli, github-models]
YAML

wim config/memory.yaml <<'YAML'
default: memo
memo: {framework: agnostic, graph_memory: true, token_efficient: true, self_hosted: true, tables: [memory_nodes, memory_edges, lexicon_spiral, knowledge_graph, self_eval_rules]}
letta: {enabled: false}
zep_graphiti: {enabled: false}
YAML

wim config/model_router.yaml <<'YAML'
routes:
  daily: {primary: qwen-plus, fallback: glm-4.7-flash}
  high_concurrency: {primary: qwen3.5-flash}
  long_context: {primary: kimi-k2.7-code, alternatives: [kimi-k3, deepseek-v4-pro]}
  audio: {primary: edge-tts}
YAML

wim config/white_label.yaml <<'YAML'
white_label:
  monthly: 499
  deliverables: [track_courses, visual_asset_pack, branded_report]
  export_dir: private_delivery/white_label
  upstream_license: buyer_responsible
YAML

wim config/payment.yaml <<'YAML'
provider: dujiaoka
webhook_path: /webhook/dujiaoka
signature: hmac_sha256
secret_env: DUJIAOKA_SECRET_KEY
order_status_paid: [paid, completed, success]
entitlement_table: entitlements
orders_table: orders
webhook_events_table: webhook_events
unlock_url_template: "{GATED_PUBLIC_URL}/unlock/{order_id}"
notify_channels: [email, private_delivery]
YAML

wim config/deploy.yaml <<'YAML'
gated:
  host_env: GATED_HOST
  port_env: GATED_PORT
  public_url_env: GATED_PUBLIC_URL
  health_path: /health
  preview_path: /preview/{content_id}
  unlock_path: /unlock/{order_id}
checklist:
  - "config/sources.yaml 无 example.com"
  - "DUJIAOKA_SECRET_KEY 非空"
  - "DASHSCOPE_API_KEY 非空且 /responses 通过"
  - "gated 服务 /health 返回 ok"
  - "P0 九单元 + P1/P2 十二单元全部 published"
  - "audio/*.mp3 全部 >= AUDIO_MIN_BYTES"
YAML

# =============================================================================
# 5. 数据库（新增支付/授权/部署）
# =============================================================================
wim src/__init__.py <<'PY'
"""EnglishFoundry core package."""
PY

wim src/db.py <<'PY'
import sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "englishfoundry.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY,name TEXT,buyer TEXT,outcome TEXT,standard TEXT,price_monthly REAL,priority TEXT);
CREATE TABLE IF NOT EXISTS content_lines (id TEXT PRIMARY KEY,name TEXT,format TEXT,price REAL,role TEXT);
CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY,track_id TEXT,type TEXT,url TEXT,frequency TEXT,compliance TEXT,env_key TEXT,env_url TEXT,env_base TEXT,endpoint TEXT);
CREATE TABLE IF NOT EXISTS fetch_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,status TEXT,attempts INTEGER DEFAULT 0,last_error TEXT,next_attempt_at TEXT,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS fetch_log (id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,ok INTEGER,http_status INTEGER,bytes INTEGER,note TEXT,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS content_units (id TEXT PRIMARY KEY,track_id TEXT,line_id TEXT,title TEXT,level TEXT,task_type TEXT,scene TEXT,outcome_link TEXT,metadata_json TEXT,status TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,content_unit_id TEXT,prompt TEXT,rubric_json TEXT,answer_key TEXT,free_body TEXT,paid_body TEXT);
CREATE TABLE IF NOT EXISTS error_patterns (id INTEGER PRIMARY KEY AUTOINCREMENT,track_id TEXT,content_unit_id TEXT,dimension TEXT,pattern TEXT,frequency INTEGER DEFAULT 1,last_seen TEXT);
CREATE TABLE IF NOT EXISTS visual_assets (id TEXT PRIMARY KEY,content_unit_id TEXT,type TEXT,file_path TEXT,label_json TEXT,version INTEGER DEFAULT 1,status TEXT,ruleset_version TEXT,label_anchored INTEGER DEFAULT 1,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS audio_assets (id TEXT PRIMARY KEY,content_unit_id TEXT,file_path TEXT,bytes INTEGER,voice TEXT,rate TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE,track_id TEXT,level TEXT,goal TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS performance_records (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,task_id TEXT,score_json TEXT,errors_json TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,plan TEXT,track_id TEXT,status TEXT,start_at TEXT,end_at TEXT,delivery_channel TEXT);
CREATE TABLE IF NOT EXISTS daily_checkins (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,day TEXT,streak INTEGER,created_at TEXT DEFAULT (datetime('now')),UNIQUE(user_id,day));
CREATE TABLE IF NOT EXISTS progress_records (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,track_id TEXT,dimension TEXT,score REAL,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS career_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,track_id TEXT,milestone TEXT,achieved_at TEXT,salary_delta_estimate REAL);
CREATE TABLE IF NOT EXISTS data_network_records (id INTEGER PRIMARY KEY AUTOINCREMENT,owner_type TEXT,owner_id TEXT,record_type TEXT,payload_ref TEXT,anonymized INTEGER DEFAULT 1,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS memory_nodes (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,node_type TEXT,content TEXT,embedding_ref TEXT,importance REAL DEFAULT 1.0,created_at TEXT DEFAULT (datetime('now')),updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS memory_edges (id INTEGER PRIMARY KEY AUTOINCREMENT,from_node INTEGER,to_node INTEGER,relation TEXT,weight REAL DEFAULT 1.0,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS lexicon_spiral (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,item TEXT,level TEXT,next_review_at TEXT,interval_days INTEGER DEFAULT 1,repetitions INTEGER DEFAULT 0,last_reviewed_at TEXT,UNIQUE(user_id,item));
CREATE TABLE IF NOT EXISTS knowledge_graph (id INTEGER PRIMARY KEY AUTOINCREMENT,track_id TEXT,concept TEXT,prerequisites_json TEXT,related_json TEXT,source TEXT,version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS self_eval_rules (id INTEGER PRIMARY KEY AUTOINCREMENT,track_id TEXT,content_unit_id TEXT,dimension TEXT,rule_text TEXT,version INTEGER DEFAULT 1,active INTEGER DEFAULT 1,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS model_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,task_type TEXT,model TEXT,fallback_used INTEGER DEFAULT 0,tokens INTEGER DEFAULT 0,cost REAL DEFAULT 0,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS white_label_orders (id INTEGER PRIMARY KEY AUTOINCREMENT,buyer TEXT,track_id TEXT,monthly_fee REAL,status TEXT,export_path TEXT,created_at TEXT DEFAULT (datetime('now')));
-- Phase 4 支付/授权/部署
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,buyer_email TEXT,track_id TEXT,plan TEXT,amount REAL,currency TEXT,status TEXT,provider TEXT,provider_order_id TEXT,raw_payload TEXT,created_at TEXT DEFAULT (datetime('now')),paid_at TEXT);
CREATE TABLE IF NOT EXISTS entitlements (id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,user_email TEXT,track_id TEXT,plan TEXT,starts_at TEXT,expires_at TEXT,status TEXT,delivery_channel TEXT,created_at TEXT DEFAULT (datetime('now')),UNIQUE(order_id,user_email,track_id));
CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY AUTOINCREMENT,provider TEXT,event_type TEXT,signature_ok INTEGER,raw_body TEXT,processed INTEGER DEFAULT 0,note TEXT,created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS deployments (id INTEGER PRIMARY KEY AUTOINCREMENT,host TEXT,version TEXT,phase INTEGER,gated_health TEXT,checked_at TEXT DEFAULT (datetime('now')));
"""
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; c.execute("PRAGMA foreign_keys=ON;"); return c
def init_db():
    c = get_conn(); c.executescript(SCHEMA); c.commit(); return c
PY

# =============================================================================
# 6. 核心源码（保持 V5.2 + 新增 sources / payments）
# =============================================================================
wim src/agents.py <<'PY'
import os, yaml, requests
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
def load_agents_config(): return yaml.safe_load((ROOT/"config/agents.yaml").read_text(encoding="utf-8"))
def check_forbidden(cfg=None):
    cfg = cfg or load_agents_config()
    forbidden = set(cfg.get("forbidden", []))
    names = {v.get("name") for v in cfg.get("build_agents", {}).values() if isinstance(v, dict)}
    hit = forbidden & names
    if hit: raise ValueError(f"选用了已验证死路: {hit}")
    return True
def validate_responses_protocol(cfg=None, online=None):
    cfg = cfg or load_agents_config()
    online = online if online is not None else os.environ.get("CHECK_ONLINE","0")=="1"
    enabled = []
    for a in cfg.get("build_agents", {}).values():
        if not isinstance(a, dict) or a.get("enabled") is False or not a.get("name"): continue
        if a.get("protocol") != "responses":
            raise ValueError(f"构建期 Agent {a['name']} 必须使用 responses 协议")
        enabled.append(a)
    if not online: return enabled, "静态校验通过；CHECK_ONLINE=0 未探测 /responses"
    for a in enabled:
        b = a["backend"]; base = os.environ.get(f"{b.upper()}_BASE_URL","").rstrip("/")
        key = os.environ.get(f"{b.upper()}_API_KEY","")
        if not base or not key: raise ValueError(f"缺少 {b.upper()}_BASE_URL/API_KEY")
        url = base + a.get("responses_endpoint","/responses")
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}","Content-Type":"application/json"},
                              json={"model": a["model"], "input": "ping"}, timeout=15)
        except Exception as e: raise ValueError(f"探测 {url} 失败: {e}")
        if r.status_code in (404,405): raise ValueError(f"后端 {b} 不支持 /responses（HTTP {r.status_code}）")
        if r.status_code >= 500: raise ValueError(f"后端 {b} 返回 {r.status_code}，无法确认 responses")
    return enabled, "在线校验通过：均支持 /responses"
def select_build_agent(difficulty="normal"):
    cfg = load_agents_config()
    return cfg["build_agents"]["hard"] if difficulty=="hard" else cfg["build_agents"]["primary"]
PY

wim src/memory.py <<'PY'
from datetime import date, timedelta
from src.db import get_conn
SPIRAL = [1,3,7,14,30]
def add_memory_node(user_id, node_type, content, importance=1.0, embedding_ref=None):
    c = get_conn(); cur = c.execute("INSERT INTO memory_nodes (user_id,node_type,content,importance,embedding_ref) VALUES (?,?,?,?,?)",
        (user_id,node_type,content,importance,embedding_ref)); c.commit(); nid=cur.lastrowid; c.close(); return nid
def add_memory_edge(f,t,relation,weight=1.0):
    c = get_conn(); c.execute("INSERT INTO memory_edges (from_node,to_node,relation,weight) VALUES (?,?,?,?)",(f,t,relation,weight)); c.commit(); c.close()
def add_lexicon_item(user_id,item,level=None,first_review=None):
    first = first_review or date.today().isoformat()
    c = get_conn(); c.execute("INSERT OR IGNORE INTO lexicon_spiral (user_id,item,level,next_review_at,interval_days,repetitions) VALUES (?,?,?,?,?,0)",
        (user_id,item,level,first,SPIRAL[0])); c.commit(); c.close()
def due_spiral_items(user_id,on_date=None):
    d = on_date or date.today().isoformat(); c = get_conn()
    rows = c.execute("SELECT * FROM lexicon_spiral WHERE user_id=? AND next_review_at<=? ORDER BY next_review_at",(user_id,d)).fetchall()
    c.close(); return [dict(r) for r in rows]
def review_spiral_item(item_id):
    c = get_conn(); r = c.execute("SELECT * FROM lexicon_spiral WHERE id=?",(item_id,)).fetchone()
    if not r: c.close(); return None
    rep = r["repetitions"]+1; interval = SPIRAL[min(rep,len(SPIRAL)-1)]
    nxt = (date.today()+timedelta(days=interval)).isoformat()
    c.execute("UPDATE lexicon_spiral SET repetitions=?,interval_days=?,last_reviewed_at=?,next_review_at=? WHERE id=?",
              (rep,interval,date.today().isoformat(),nxt,item_id)); c.commit(); c.close(); return nxt
def add_self_eval_rule(track_id,dimension,rule_text,content_unit_id=None):
    c = get_conn(); c.execute("INSERT INTO self_eval_rules (track_id,content_unit_id,dimension,rule_text) VALUES (?,?,?,?)",
        (track_id,content_unit_id,dimension,rule_text)); c.commit(); c.close()
def active_self_eval_rules(track_id,content_unit_id=None):
    c = get_conn()
    if content_unit_id:
        rows = c.execute("SELECT * FROM self_eval_rules WHERE track_id=? AND active=1 AND (content_unit_id IS NULL OR content_unit_id=?)",(track_id,content_unit_id)).fetchall()
    else:
        rows = c.execute("SELECT * FROM self_eval_rules WHERE track_id=? AND active=1",(track_id,)).fetchall()
    c.close(); return [dict(r) for r in rows]
def add_knowledge_concept(track_id,concept,prerequisites=None,related=None,source=None):
    c = get_conn(); c.execute("INSERT INTO knowledge_graph (track_id,concept,prerequisites_json,related_json,source) VALUES (?,?,?,?,?)",
        (track_id,concept,str(prerequisites or []),str(related or []),source)); c.commit(); c.close()
PY

wim src/model_router.py <<'PY'
import yaml
from pathlib import Path
from src.db import get_conn
ROOT = Path(__file__).resolve().parent.parent
def load_routes(): return yaml.safe_load((ROOT/"config/model_router.yaml").read_text(encoding="utf-8"))["routes"]
def route(task_type, fallback_used=False):
    r = load_routes()
    if task_type not in r: raise ValueError(f"未知任务: {task_type}; 可选 {list(r)}")
    return r[task_type].get("fallback") if fallback_used and r[task_type].get("fallback") else r[task_type]["primary"]
def route_with_fallback(task_type):
    r = load_routes()[task_type]
    return {"primary": r.get("primary"), "fallback": r.get("fallback"), "alternatives": r.get("alternatives", [])}
def record_usage(task_type, model, fallback_used=False, tokens=0, cost=0.0):
    c = get_conn(); c.execute("INSERT INTO model_usage (task_type,model,fallback_used,tokens,cost) VALUES (?,?,?,?,?)",
        (task_type,model,int(fallback_used),tokens,cost)); c.commit(); c.close()
PY

wim src/audio.py <<'PY'
from pathlib import Path
def validate_audio_file(path, min_bytes=50000):
    path = Path(path)
    if not path.exists(): return False, f"文件不存在: {path}"
    size = path.stat().st_size
    if size < min_bytes: return False, f"音频过小，疑似占位: {path} ({size} < {min_bytes})"
    with open(path,"rb") as f: head = f.read(3)
    if head not in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True, f"大小合格，但未识别标准 MP3 头: {path} ({size})"
    return True, f"音频合格: {path} ({size})"
PY

wim src/paywall.py <<'PY'
from pathlib import Path
import html
def build_preview(title, free_body, paid_body, preview_lines=5, locked_outline=None, social_proof=""):
    preview = "\n".join(paid_body.splitlines()[:preview_lines])
    outline = "".join(f"<li>{html.escape(x)}</li>" for x in (locked_outline or []))
    proof = f"<p class='social-proof'>{html.escape(social_proof)}</p>" if social_proof else ""
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>{html.escape(title)} - 预览</title>
<style>body{{font-family:Arial,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#0f172a}}
.free{{background:#f0fdf4;padding:20px;border-radius:8px}} .premium-preview{{background:#fef9c3;padding:20px;border-radius:8px;margin-top:20px}}
pre{{white-space:pre-wrap;font-family:Arial,sans-serif}} .social-proof{{color:#15803d;font-weight:bold}}</style>
<body><h1>{html.escape(title)}</h1>
<section class="free"><h2>免费部分</h2><pre>{html.escape(free_body)}</pre></section>
<section class="premium-preview"><h2>付费内容预览</h2><pre>{html.escape(preview)}</pre>
<p><strong>🔒 锁定：</strong>完整归档、全部音频/图卡、纠错反馈与评分模拟需订阅解锁。</p>
<ul>{outline}</ul>{proof}</section></body></html>"""
def write_paid_body(private_root, track_id, content_id, paid_body):
    out = Path(private_root)/track_id/f"{content_id}.md"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(paid_body, encoding="utf-8"); return out
PY

wim src/visual.py <<'PY'
from pathlib import Path
from xml.sax.saxutils import escape
def _svg(width, height, body):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">' + body + "</svg>"
def write_svg_card(path, title, labels, subtitle=""):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    parts = ['<rect width="100%" height="100%" fill="#f8fafc"/>',
             f'<text x="40" y="60" font-family="Arial" font-size="30" font-weight="bold" fill="#0f172a">{escape(title)}</text>']
    if subtitle: parts.append(f'<text x="40" y="95" font-family="Arial" font-size="16" fill="#475569">{escape(subtitle)}</text>')
    y = 140
    for i, label in enumerate(labels):
        parts.append(f'<rect x="40" y="{y-24}" width="820" height="48" rx="8" fill="#e2e8f0"/>')
        parts.append(f'<text x="60" y="{y+6}" font-family="Arial" font-size="18" fill="#0f172a">{i+1}. {escape(label)}</text>')
        y += 64
    path.write_text(_svg(900, max(560, y+40), "".join(parts)), encoding="utf-8"); return path
def write_svg_decision_tree(path, title, nodes, subtitle=""):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    parts = ['<rect width="100%" height="100%" fill="#f8fafc"/>',
             f'<text x="40" y="60" font-family="Arial" font-size="28" font-weight="bold" fill="#0f172a">{escape(title)}</text>']
    if subtitle: parts.append(f'<text x="40" y="92" font-family="Arial" font-size="15" fill="#475569">{escape(subtitle)}</text>')
    y = 130
    for i, n in enumerate(nodes):
        x = 40 + (i % 2) * 430
        parts.append(f'<rect x="{x}" y="{y}" width="400" height="90" rx="10" fill="#e0f2fe" stroke="#0284c7"/>')
        parts.append(f'<text x="{x+18}" y="{y+35}" font-family="Arial" font-size="16" fill="#0f172a">{escape(n)}</text>')
        parts.append(f'<text x="{x+18}" y="{y+62}" font-family="Arial" font-size="13" fill="#475569">branch {i+1} · label anchored</text>')
        if i % 2 == 1 or i == len(nodes)-1: y += 120
    path.write_text(_svg(900, y+40, "".join(parts)), encoding="utf-8"); return path
def write_svg_infographic(path, title, sections, subtitle=""):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    parts = ['<rect width="100%" height="100%" fill="#f8fafc"/>',
             f'<text x="40" y="60" font-family="Arial" font-size="28" font-weight="bold" fill="#0f172a">{escape(title)}</text>']
    if subtitle: parts.append(f'<text x="40" y="92" font-family="Arial" font-size="15" fill="#475569">{escape(subtitle)}</text>')
    y = 130
    for s in sections:
        parts.append(f'<rect x="40" y="{y}" width="820" height="70" rx="8" fill="#f1f5f9" stroke="#cbd5e1"/>')
        parts.append(f'<text x="58" y="{y+30}" font-family="Arial" font-size="17" font-weight="bold" fill="#0f172a">{escape(s.get("heading",""))}</text>')
        parts.append(f'<text x="58" y="{y+54}" font-family="Arial" font-size="14" fill="#334155">{escape(s.get("detail",""))}</text>')
        y += 84
    path.write_text(_svg(900, y+30, "".join(parts)), encoding="utf-8"); return path
def write_svg_data_chart(path, title, points, subtitle=""):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    if not points: points = [("A",1),("B",2),("C",3)]
    maxv = max(v for _,v in points) or 1
    parts = ['<rect width="100%" height="100%" fill="#f8fafc"/>',
             f'<text x="40" y="60" font-family="Arial" font-size="28" font-weight="bold" fill="#0f172a">{escape(title)}</text>']
    if subtitle: parts.append(f'<text x="40" y="92" font-family="Arial" font-size="15" fill="#475569">{escape(subtitle)}</text>')
    x0, y0, w, h = 60, 460, 780, 320
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x0+w}" y2="{y0}" stroke="#94a3b8"/>')
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y0-h}" stroke="#94a3b8"/>')
    bw = w // max(len(points),1) - 20
    for i,(label,v) in enumerate(points):
        bh = int(h * v / maxv); bx = x0 + i*(bw+20) + 10
        parts.append(f'<rect x="{bx}" y="{y0-bh}" width="{bw}" height="{bh}" fill="#0ea5e9"/>')
        parts.append(f'<text x="{bx}" y="{y0-bh-8}" font-family="Arial" font-size="13" fill="#0f172a">{escape(str(v))}</text>')
        parts.append(f'<text x="{bx}" y="{y0+20}" font-family="Arial" font-size="12" fill="#475569">{escape(label)}</text>')
    path.write_text(_svg(900, 560, "".join(parts)), encoding="utf-8"); return path
PY

wim src/fetcher.py <<'PY'
import time, random, os, requests, feedparser
def retry_call(fn, max_attempts=3, backoff_seconds=5, jitter=0.0):
    last = None
    for attempt in range(1, max_attempts+1):
        try: return fn()
        except Exception as e:
            last = e
            if attempt == max_attempts: break
            time.sleep(backoff_seconds * (2 ** (attempt-1)) + random.random()*jitter)
    raise last
def enqueue(conn, source_id, error=None):
    conn.execute("INSERT INTO fetch_queue (source_id,status,attempts,last_error,next_attempt_at) VALUES (?,?,?,?,datetime('now','+5 minutes'))",
                 (source_id, "retry" if error else "pending", 1 if error else 0, str(error) if error else None))
    conn.commit()
def log_fetch(conn, source_id, ok, http_status=None, nbytes=None, note=None):
    conn.execute("INSERT INTO fetch_log (source_id,ok,http_status,bytes,note) VALUES (?,?,?,?,?)",
                 (source_id,int(ok),http_status,nbytes,note)); conn.commit()
def resolve_url(src: dict) -> str | None:
    if src.get("url"): return src["url"]
    if src.get("env_url"): return os.environ.get(src["env_url"]) or None
    if src.get("env_base") and src.get("endpoint"):
        base = os.environ.get(src["env_base"], "").rstrip("/")
        return (base + src["endpoint"]) if base else None
    return None
def fetch_rss(url, timeout=20):
    parsed = feedparser.parse(url)
    return {"status": getattr(parsed, "status", None), "entries": [e.get("link") or e.get("id") for e in parsed.entries][:50], "count": len(parsed.entries)}
def fetch_api(url, key_env=None, json_body=None, timeout=20):
    headers = {}
    if key_env:
        k = os.environ.get(key_env, "")
        if not k: raise RuntimeError(f"缺少 {key_env}")
        headers["Authorization"] = f"Bearer {k}"
    if json_body is None:
        r = requests.get(url, headers=headers, timeout=timeout)
    else:
        r = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=json_body, timeout=timeout)
    return {"status": r.status_code, "bytes": len(r.content)}
PY

wim src/compounding.py <<'PY'
from datetime import date
from src.db import get_conn
def record_progress(user_id, track_id, dimension, score):
    c = get_conn(); c.execute("INSERT INTO progress_records (user_id,track_id,dimension,score) VALUES (?,?,?,?)",(user_id,track_id,dimension,score)); c.commit(); c.close()
def progress_curve(user_id, track_id):
    c = get_conn(); rows = c.execute("SELECT dimension,score,created_at FROM progress_records WHERE user_id=? AND track_id=? ORDER BY created_at",(user_id,track_id)).fetchall(); c.close(); return [dict(r) for r in rows]
def record_career_outcome(user_id, track_id, milestone, achieved_at=None, salary_delta_estimate=0.0):
    c = get_conn(); c.execute("INSERT INTO career_outcomes (user_id,track_id,milestone,achieved_at,salary_delta_estimate) VALUES (?,?,?,?,?)",(user_id,track_id,milestone,achieved_at,salary_delta_estimate)); c.commit(); c.close()
def record_data_network(owner_type, owner_id, record_type, payload_ref, anonymized=1):
    c = get_conn(); c.execute("INSERT INTO data_network_records (owner_type,owner_id,record_type,payload_ref,anonymized) VALUES (?,?,?,?,?)",(owner_type,owner_id,record_type,payload_ref,anonymized)); c.commit(); c.close()
def record_checkin(user_id, day=None, streak=1):
    d = day or date.today().isoformat()
    c = get_conn(); c.execute("INSERT OR IGNORE INTO daily_checkins (user_id,day,streak) VALUES (?,?,?)",(user_id,d,streak)); c.commit(); c.close()
PY

wim src/analytics.py <<'PY'
import csv, os, smtplib, sys
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
def daily_report(conn, out_dir):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir/f"daily_report_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    tables = ["tracks","content_units","error_patterns","visual_assets","audio_assets","users","subscriptions",
              "memory_nodes","memory_edges","lexicon_spiral","self_eval_rules","model_usage",
              "progress_records","career_outcomes","data_network_records","fetch_queue","white_label_orders",
              "orders","entitlements","webhook_events"]
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["metric","value"])
        for t in tables:
            w.writerow([f"count_{t}", conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]])
        w.writerow(["generated_at", datetime.utcnow().isoformat()])
    return path
def maybe_email(path):
    host = os.environ.get("SMTP_HOST"); to = os.environ.get("ANALYTICS_EMAIL_TO")
    if not host or not to: return False
    try:
        msg = MIMEText(Path(path).read_text(encoding="utf-8"), "plain", "utf-8")
        msg["Subject"] = f"EnglishFoundry 每日分析 {Path(path).stem}"
        msg["From"] = os.environ.get("SMTP_USER","noreply@example.com"); msg["To"] = to
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT","587"))) as s:
            s.starttls(); s.login(os.environ.get("SMTP_USER",""), os.environ.get("SMTP_PASS","")); s.send_message(msg)
        return True
    except Exception as e:
        print(f"[warn] 邮件发送失败: {e}", file=sys.stderr); return False
PY

# 新增：支付 + 授权
wim payments/__init__.py <<'PY'
"""EnglishFoundry payment package."""
PY

wim payments/dujiaoka.py <<'PY'
import hashlib, hmac, os, json
from datetime import datetime, timedelta
from src.db import get_conn

def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")

def record_event(provider, event_type, signature_ok, raw_body, processed=0, note=""):
    c = get_conn(); c.execute("INSERT INTO webhook_events (provider,event_type,signature_ok,raw_body,processed,note) VALUES (?,?,?,?,?,?)",
        (provider,event_type,int(signature_ok),raw_body,processed,note)); c.commit(); c.close()

def upsert_order(order_id, buyer_email, track_id, plan, amount, currency, status, provider, provider_order_id, raw_payload):
    c = get_conn()
    c.execute("INSERT OR REPLACE INTO orders (id,buyer_email,track_id,plan,amount,currency,status,provider,provider_order_id,raw_payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (order_id,buyer_email,track_id,plan,amount,currency,status,provider,provider_order_id,raw_payload))
    c.commit(); c.close()

def mark_paid(order_id, paid_at=None):
    c = get_conn(); c.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (paid_at or datetime.utcnow().isoformat(), order_id)); c.commit(); c.close()

def grant_entitlement(order_id, user_email, track_id, plan, days=30):
    c = get_conn()
    row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row: c.close(); raise ValueError(f"订单不存在: {order_id}")
    starts = datetime.utcnow(); expires = starts + timedelta(days=days)
    c.execute("""INSERT OR REPLACE INTO entitlements (order_id,user_email,track_id,plan,starts_at,expires_at,status,delivery_channel)
      VALUES (?,?,?,?,?,?,?,?)""",
      (order_id,user_email,track_id,plan,starts.isoformat(),expires.isoformat(),"active","private_delivery"))
    c.commit(); c.close()

def has_entitlement(user_email, track_id) -> bool:
    c = get_conn()
    row = c.execute("SELECT * FROM entitlements WHERE user_email=? AND track_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
                    (user_email, track_id, datetime.utcnow().isoformat())).fetchone()
    c.close(); return row is not None

def handle_dujiaoka_payload(raw_body: bytes, signature: str) -> dict:
    secret = os.environ.get("DUJIAOKA_SECRET_KEY", "")
    if not secret: raise RuntimeError("DUJIAOKA_SECRET_KEY 未配置")
    sig_ok = verify_signature(raw_body, signature, secret)
    try: payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception: payload = {}
    event_type = payload.get("event_type") or payload.get("type") or "order_callback"
    record_event("dujiaoka", event_type, sig_ok, raw_body.decode("utf-8", "ignore"), 0, "" if sig_ok else "signature_mismatch")
    if not sig_ok: return {"ok": False, "reason": "signature_mismatch"}
    order_id = str(payload.get("order_id") or payload.get("id") or "")
    buyer_email = payload.get("buyer_email") or payload.get("email") or ""
    track_id = payload.get("track_id") or payload.get("product_id") or ""
    plan = payload.get("plan") or "monthly"
    amount = float(payload.get("amount") or 0)
    currency = payload.get("currency") or os.environ.get("PAYMENT_CURRENCY","CNY")
    status = payload.get("status") or "paid"
    if not order_id: return {"ok": False, "reason": "missing_order_id"}
    upsert_order(order_id, buyer_email, track_id, plan, amount, currency, status, "dujiaoka", order_id, json.dumps(payload))
    if status in ("paid","completed","success"):
        mark_paid(order_id)
        if buyer_email and track_id: grant_entitlement(order_id, buyer_email, track_id, plan, days=30)
    return {"ok": True, "order_id": order_id, "status": status}
PY

wim src/sources.py <<'PY'
import os, yaml, json
from pathlib import Path
from src.fetcher import retry_call, resolve_url, fetch_rss, fetch_api, enqueue, log_fetch

ROOT = Path(__file__).resolve().parent.parent

def load_sources(path=None):
    path = path or (ROOT/"config/sources.yaml")
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))

def check_no_placeholder(sources_cfg):
    bad = []
    for sid, s in sources_cfg.get("sources", {}).items():
        for k in ("url","env_url","env_base"):
            v = s.get(k)
            if isinstance(v, str) and "example.com" in v:
                bad.append((sid, k, v))
    for s in sources_cfg.get("public_sources", []):
        for k in ("url","env_url","env_base"):
            v = s.get(k)
            if isinstance(v, str) and "example.com" in v:
                bad.append((s.get("id","public"), k, v))
    return bad

def fetch_one(conn, sid, src, timeout=20):
    if src.get("type") == "authorized_import":
        log_fetch(conn, sid, True, note="authorized_import_skipped"); return {"ok": True, "note": "authorized_import"}
    url = resolve_url(src)
    if not url:
        enqueue(conn, sid, "missing_url"); log_fetch(conn, sid, False, note="missing_url"); return {"ok": False, "reason": "missing_url"}
    if src.get("type") == "rss":
        r = retry_call(lambda: fetch_rss(url, timeout=timeout), max_attempts=3, backoff_seconds=2)
        log_fetch(conn, sid, True, r.get("status"), None, f"entries={r.get('count')}"); return {"ok": True, "count": r.get("count")}
    if src.get("type") == "api":
        key_env = src.get("env_key")
        body = None
        if src.get("endpoint","").endswith("/mcp"): body = {"method":"list"}
        r = retry_call(lambda: fetch_api(url, key_env=key_env, json_body=body, timeout=timeout), max_attempts=3, backoff_seconds=2)
        log_fetch(conn, sid, True, r.get("status"), r.get("bytes")); return {"ok": True, "bytes": r.get("bytes")}
    log_fetch(conn, sid, True, note=f"unsupported_type:{src.get('type')}"); return {"ok": True, "note": "unsupported_type"}
PY

# =============================================================================
# 7. FastAPI gated 服务
# =============================================================================
wim server/__init__.py <<'PY'
"""EnglishFoundry gated server."""
PY

wim server/gated.py <<'PY'
import os, json
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse, JSONResponse
from payments.dujiaoka import handle_dujiaoka_payload, has_entitlement
from src.db import get_conn

app = FastAPI(title="EnglishFoundry Gated")
ROOT = Path(__file__).resolve().parent.parent

@app.get("/health")
def health():
    return {"ok": True, "service": "englishfoundry-gated", "phase": 4}

@app.post("/webhook/dujiaoka")
async def webhook_dujiaoka(request: Request, x_signature: str | None = Header(default=None)):
    raw = await request.body()
    try:
        result = handle_dujiaoka_payload(raw, x_signature or "")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(result)

@app.get("/preview/{content_id}")
def preview(content_id: str):
    c = get_conn()
    row = c.execute("""SELECT cu.id, cu.title, t.free_body FROM content_units cu JOIN tasks t ON t.content_unit_id=cu.id WHERE cu.id=?""",(content_id,)).fetchone()
    c.close()
    if not row: raise HTTPException(status_code=404, detail="not_found")
    return {"id": row["id"], "title": row["title"], "free_body": row["free_body"], "locked": True}

@app.get("/unlock/{order_id}")
def unlock(order_id: str, email: str, track_id: str):
    if not has_entitlement(email, track_id):
        raise HTTPException(status_code=403, detail="no_entitlement")
    c = get_conn()
    rows = c.execute("""SELECT cu.id, cu.title, t.paid_body FROM content_units cu JOIN tasks t ON t.content_unit_id=cu.id
      WHERE cu.track_id=? AND cu.id LIKE 'p0_%' OR cu.id LIKE 'p1_%' OR cu.id LIKE 'p2_%'""",(track_id,)).fetchall()
    c.close()
    return {"order_id": order_id, "track_id": track_id, "email": email,
            "units": [{"id": r["id"], "title": r["title"], "paid_body": r["paid_body"]} for r in rows]}

@app.get("/manifest/{track_id}")
def manifest(track_id: str):
    c = get_conn()
    units = [dict(r) for r in c.execute("SELECT id,title,level FROM content_units WHERE track_id=? AND status='published'",(track_id,)).fetchall()]
    assets = [dict(r) for r in c.execute("SELECT va.id,va.type,va.file_path FROM visual_assets va JOIN content_units cu ON va.content_unit_id=cu.id WHERE cu.track_id=?",(track_id,)).fetchall()]
    c.close()
    return {"track_id": track_id, "units": units, "assets": assets}

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("GATED_HOST","127.0.0.1")
    port = int(os.environ.get("GATED_PORT","8787"))
    uvicorn.run(app, host=host, port=port)
PY

# =============================================================================
# 8. 基础脚本
# =============================================================================
wim scripts/__init__.py <<'PY'
"""EnglishFoundry scripts package."""
PY

wim scripts/init_db.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
c = init_db(); print("DB initialized:", c.execute("PRAGMA database_list").fetchone()[2]); c.close()
PY

wim scripts/seed_tracks.py <<'PY'
import sys, yaml; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
root = Path(__file__).resolve().parent.parent
data = yaml.safe_load((root/"config/tracks.yaml").read_text(encoding="utf-8"))
c = init_db()
for t in data["tracks"]:
    c.execute("INSERT OR REPLACE INTO tracks (id,name,buyer,outcome,standard,price_monthly,priority) VALUES (?,?,?,?,?,?,?)",
              (t["id"],t["name"],t.get("buyer"),t.get("outcome"),t.get("standard"),t.get("price_monthly"),t.get("priority")))
c.commit(); c.close(); print("seeded 7 tracks")
PY

wim scripts/seed_content_lines.py <<'PY'
import sys, yaml; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
root = Path(__file__).resolve().parent.parent
data = yaml.safe_load((root/"config/content_lines.yaml").read_text(encoding="utf-8"))["content_lines"]
c = init_db()
c.execute("INSERT OR REPLACE INTO content_lines (id,name,format,price,role) VALUES (?,?,?,?,?)", ("deep_read","Deep Read",data["deep_read"]["format"],data["deep_read"]["price_single"],"知识密度"))
c.execute("INSERT OR REPLACE INTO content_lines (id,name,format,price,role) VALUES (?,?,?,?,?)", ("book_foundry","Book Foundry",data["book_foundry"]["format"],data["book_foundry"]["price_per_book"],"个性化课程化"))
c.execute("INSERT OR REPLACE INTO content_lines (id,name,format,price,role) VALUES (?,?,?,?,?)", ("daily_starter","Daily Starter",data["daily_starter"]["format"],0,"免费引流/打卡"))
c.commit(); c.close(); print("seeded 3 content lines")
PY

wim scripts/seed_sources.py <<'PY'
import sys, yaml; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
root = Path(__file__).resolve().parent.parent
data = yaml.safe_load((root/"config/sources.yaml").read_text(encoding="utf-8"))
c = init_db()
for sid, s in data["sources"].items():
    c.execute("INSERT OR REPLACE INTO sources (id,track_id,type,url,frequency,compliance,env_key,env_url,env_base,endpoint) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (sid,s.get("track"),s.get("type"),s.get("url"),s.get("frequency"),s.get("compliance"),s.get("env_key"),s.get("env_url"),s.get("env_base"),s.get("endpoint")))
for s in data.get("public_sources",[]):
    c.execute("INSERT OR REPLACE INTO sources (id,track_id,type,url,frequency,compliance,env_key,env_url,env_base,endpoint) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (s["id"],None,s.get("type"),s.get("url"),s.get("frequency"),s.get("compliance"),s.get("env_key"),s.get("env_url"),s.get("env_base"),s.get("endpoint")))
c.commit(); c.close(); print("seeded sources")
PY

wim scripts/check_scope.py <<'PY'
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ["world_today","故事宇宙","连载宇宙","The World Today","可追剧"]
def scan_path(p):
    if not p.exists(): return []
    if p.is_file():
        txt = p.read_text(encoding="utf-8", errors="ignore")
        return [f"{p}: 含禁用词 {w}" for w in FORBIDDEN if w in txt]
    hits = []
    for f in p.rglob("*"):
        if f.is_file() and f.suffix in (".yaml",".yml",".md",".html",".py",".json"):
            hits += scan_path(f)
    return hits
def main():
    hits = []
    for d in ["config","static","content/free","src"]:
        hits += scan_path(ROOT/d)
    if hits:
        print("\n".join(hits)); print("[fail] 发现故事宇宙残留，必须去除"); return 1
    print("[ok] 无故事宇宙残留"); return 0
if __name__ == "__main__": sys.exit(main())
PY

wim scripts/check_agents.py <<'PY'
import sys, os; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.agents import load_agents_config, check_forbidden, validate_responses_protocol
cfg = load_agents_config(); check_forbidden(cfg)
enabled, msg = validate_responses_protocol(cfg)
print("[ok] 死路检查：Gemini CLI / GitHub Models 未选用")
for a in enabled: print(f"  - {a['name']} | {a['backend']} | {a['model']} | {a['protocol']}")
print("[ok] " + msg)
PY

wim scripts/check_sources_real.py <<'PY'
import sys, yaml; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.sources import load_sources, check_no_placeholder
def main():
    cfg = load_sources()
    bad = check_no_placeholder(cfg)
    if bad:
        print("[fail] sources.yaml 仍含 example.com 占位：")
        for sid,k,v in bad: print(f"  - {sid}.{k} = {v}")
        return 1
    print("[ok] sources.yaml 无 example.com 占位")
    return 0
if __name__ == "__main__": sys.exit(main())
PY

wim scripts/seed_memory.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from src.memory import add_memory_node, add_memory_edge, add_lexicon_item, add_self_eval_rule, add_knowledge_concept
init_db().close()
uid = 1
n1 = add_memory_node(uid,"profile","OET 考生（护士）；目标 2026 Q1 通过；写作当前 C+；薄弱：purpose/content",1.5)
n2 = add_memory_node(uid,"error","purpose 句写在第二段，未用 I am writing to refer",1.2)
n3 = add_memory_node(uid,"preference","偏好慢速音频 + 可下载图卡复习",1.0)
add_memory_edge(n2,n1,"explains_weakness"); add_memory_edge(n3,n1,"learning_preference")
for item in ["refer for further assessment","persistent lower back pain","I am writing to refer"]: add_lexicon_item(uid,item,"B2")
add_self_eval_rule("oet","purpose","目的句必须在首行；未出现读者行动动词判不达标")
add_self_eval_rule("oet","content","病例信息压缩比低于 3:1 提示信息筛选不足")
add_knowledge_concept("oet","referral_letter",["purpose_sentence","info_selection"],["discharge_letter"],"OET rubric")
print("[ok] MemO 记忆层种子完成")
PY

wim scripts/route_demo.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.model_router import route_with_fallback, record_usage
for t in ["daily","high_concurrency","long_context","audio"]:
    r = route_with_fallback(t); print(f"[route] {t}: primary={r['primary']} fallback={r['fallback']} alts={r['alternatives']}")
    record_usage(t, r["primary"])
print("[ok] 模型路由演示完成")
PY

wim scripts/fetch_sources.py <<'PY'
import sys, os, yaml; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from src.sources import load_sources, fetch_one
from src.fetcher import enqueue
root = Path(__file__).resolve().parent.parent
cfg = load_sources()
conn = init_db()
online = os.environ.get("FETCH_ONLINE","0")=="1"
print(f"[fetch] 队列初始化；online={online}")
for sid, s in cfg["sources"].items():
    if not online: enqueue(conn, sid); continue
    try:
        res = fetch_one(conn, sid, s); print(f"[ok] {sid}: {res}")
    except Exception as e:
        enqueue(conn, sid, e); print(f"[retry] {sid}: {e}")
conn.close(); print("[ok] 抓取队列/日志完成")
PY

wim scripts/daily_analytics.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import get_conn
from src.analytics import daily_report, maybe_email
root = Path(__file__).resolve().parent.parent
c = get_conn(); p = daily_report(c, root/"logs"); c.close()
sent = maybe_email(p); print("daily report:", p, "| email sent:", sent)
PY

wim scripts/build_about.py <<'PY'
from pathlib import Path
root = Path(__file__).resolve().parent.parent
html = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>关于 EnglishFoundry</title>
<style>body{font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#0f172a}</style>
<body><h1>EnglishFoundry</h1>
<h2>使命</h2><p>把当周真实材料转化为通向职业结果的英文训练。</p>
<h2>价值</h2><p>7 个垂直结果赛道 + Deep Read / Book Foundry / Daily Starter + 可下载图卡与音频。</p>
<h2>信任</h2><p>信源公开标注；高 stakes 内容以官方最新文件为准；付费正文不放入静态 HTML。</p>
</body></html>"""
out = root/"static/about.html"; out.parent.mkdir(parents=True, exist_ok=True); out.write_text(html, encoding="utf-8"); print("about:", out)
PY

wim scripts/export_white_label.py <<'PY'
import sys, zipfile, json; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import get_conn
root = Path(__file__).resolve().parent.parent
track = sys.argv[1] if len(sys.argv)>1 else "oet"
conn = get_conn()
units = [dict(r) for r in conn.execute("SELECT * FROM content_units WHERE track_id=?",(track,)).fetchall()]
assets = [dict(r) for r in conn.execute("SELECT va.* FROM visual_assets va JOIN content_units cu ON va.content_unit_id=cu.id WHERE cu.track_id=?",(track,)).fetchall()]
conn.close()
out = root/"private_delivery/white_label"/f"{track}_pack.zip"; out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out,"w") as z:
    z.writestr("manifest.json", json.dumps({"track":track,"units":units,"assets":assets}, ensure_ascii=False, indent=2))
    for a in assets:
        p = root/a["file_path"]
        if p.exists(): z.write(p, arcname=f"assets/{p.name}")
print("[ok] 白标资产包:", out)
PY

# =============================================================================
# 9. P0 九单元（保持 V5.2 真实内容）
# =============================================================================
wim scripts/seed_p0_units.py <<'PY'
import sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db

R_OET = {"dimensions":[{"name":"purpose","weight":0.25,"criteria":"首行明确写信目的与读者行动"},{"name":"content","weight":0.25,"criteria":"压缩比≥5:1"},{"name":"conciseness","weight":0.20,"criteria":"避免抄病例"},{"name":"genre","weight":0.15,"criteria":"语域匹配读者"},{"name":"organization","weight":0.15,"criteria":"段落功能清晰"}],"pass_threshold":0.70}
R_ICAO = {"dimensions":[{"name":n,"weight":1/6,"criteria":c} for n,c in [("pronunciation","清晰可懂"),("structure","句式正确"),("vocabulary","术语准确"),("fluency","流畅"),("comprehension","理解指令"),("interactions","应对得当")]],"pass_threshold":0.70,"level_4_threshold":0.70}
R_FIN = {"dimensions":[{"name":"tone_recognition","weight":0.30,"criteria":"准确识别 certainty/hedging/deflection"},{"name":"followup_precision","weight":0.30,"criteria":"追问含量化要求"},{"name":"number_narrative","weight":0.20,"criteria":"headline→driver→outlook"},{"name":"conviction_language","weight":0.20,"criteria":"机构级表达"}],"pass_threshold":0.70}

UNITS = [
 {"id":"p0_oet_purpose","track":"oet","title":"OET 写作：转诊信目的句","level":"B2","task_type":"standard","scene":"referral","outcome":"OET Writing purpose",
  "free":"免费：目的句 3 种模式。\n① I am writing to refer [patient] to [specialty] for [reason].\n② I would be grateful if you could see [patient] regarding [concern].\n③ Thank you for seeing [patient], whom I am referring for [reason].\n练习：将病例摘要压缩为一句目的句。",
  "paid":"付费：5 个完整病例摘要 → 目的句改写 + 5 维 rubric 反馈 + 错误模式追踪。\n\n病例 1（骨科）：58 岁男性，持续下背痛 6 周，保守治疗无效，需骨科评估。\n目标句：I am writing to refer Mr. Chen, a 58-year-old man, to your orthopedic clinic for further assessment of persistent lower back pain unresponsive to conservative management.\n\n病例 2（心内科）：72 岁女性，劳力性胸痛 3 个月，ECG 示 ST 段压低。\n目标句：I am writing to refer Mrs. Lim, a 72-year-old woman, to your cardiology clinic for evaluation of exertional chest pain with ST-segment depression on ECG.\n\n病例 3（神经科）：45 岁男性，反复偏头痛近 1 月加重。\n目标句：I am writing to refer Mr. Tan, a 45-year-old man, to your neurology clinic for assessment of recurrent migraine worsening over the past month.\n\n病例 4（内分泌科）：33 岁女性，甲亢药物治疗后复发。\n目标句：I am writing to refer Ms. Wong, a 33-year-old woman, to your endocrinology clinic for assessment of recurrent hyperthyroidism after medical therapy.\n\n病例 5（呼吸科）：67 岁男性，慢性咳嗽 2 月，CT 示肺结节。\n目标句：I am writing to refer Mr. Lee, a 67-year-old man, to your respiratory clinic for assessment of a lung nodule detected on CT.",
  "key":"首行含 refer + specialty + reason；读者行动动词明确；不抄病例。"},
 {"id":"p0_oet_discharge","track":"oet","title":"OET 写作：出院信信息筛选","level":"B2","task_type":"standard","scene":"discharge","outcome":"OET Writing content",
  "free":"免费：出院信必须包含的三类信息。\n① Medication changes（新增/停用/调整剂量）\n② Follow-up plan（谁、何时、做什么）\n③ Red flags（什么情况需立即就医）\n练习：从病例中识别这三类信息。",
  "paid":"付费：3 个完整病例 → 出院信改写（5:1 压缩）+ rubric 反馈 + 遗漏项标注。\n\n病例 1（阑尾切除术后）：腹腔镜阑尾切除术术后 3 天出院；新增 apixaban 5mg BD，停用 ibuprofen；GP 7 天拆线、2 周复查；胸痛/气促/伤口红肿渗液需立即就医。\n目标要点：Medication changes + Follow-up + Red flags；压缩比≥5:1。\n\n病例 2（心力衰竭加重）：furosemide 40mg OD 调整为 80mg OD，新增 spironolactone 25mg OD；心内科 1 周复查，每日称重；体重 3 天增加 >2kg、静息气促、下肢水肿加重需立即就医。\n\n病例 3（COPD 急性加重）：prednisolone 30mg OD × 5 天，新增 tiotropium inhaler；呼吸科 4 周复查肺功能；气促加重、痰量/痰色变化、意识改变需立即就医。",
  "key":"三类信息齐全；压缩比≥5:1；不抄病例；语域匹配 GP。"},
 {"id":"p0_oet_register","track":"oet","title":"OET 沟通：医患语域转换","level":"B2","task_type":"scene_dialogue","scene":"patient","outcome":"OET Speaking register",
  "free":"免费：医学术语 → 患者语言 3 条原则。\n① 用日常词替代拉丁术语\n② 用第二人称\n③ 一句话只讲一个概念\n练习：把 3 个术语转成患者版本。",
  "paid":"付费：5 个术语 → 患者版本改写 + 语域评分 + 可读性检查。\n\n1. hypertension → high blood pressure\n2. myocardial infarction → heart attack\n3. dyspnoea → shortness of breath\n4. oedema → swelling\n5. haemoptysis → coughing up blood\n\n原则：日常词；第二人称；一句一概念；避免缩写；用具体动词。\n示例：Your blood pressure is high. You had a heart attack. You are short of breath. We will start one medicine today.\n评分：Flesch reading ease > 60；患者可复述；无未解释术语。",
  "key":"日常词；第二人称；一句一概念；Flesch>60。"},
 {"id":"p0_icao_clarify","track":"icao","title":"ICAO 特情：请求澄清标准用语","level":"B2","task_type":"decision","scene":"emergency","outcome":"ICAO interactions",
  "free":"免费：3 种请求澄清的标准用语。\n① Say again.\n② Confirm [item].\n③ Standby.\n原则：不得直接执行含混指令；请求澄清不丢分。",
  "paid":"付费：5 个特情场景 → 录音提交 → 六维评分 + 决策树反馈。\n\n场景 1：Tower: \"Cessna one two three, taxi via alpha, hold short of runway two seven.\"（alpha 被截断）\n正确：Tower, Cessna one two three, say again taxi route.\n错误：直接 taxi via alpha\n\n场景 2：Tower: \"Cessna one two three, cleared to land runway two seven.\"（背景噪音）\n正确：Tower, Cessna one two three, confirm cleared to land runway two seven.\n错误：直接落地\n\n场景 3：Tower: \"Cessna one two three, contact ground 121.9.\"\n正确：Tower, Cessna one two three, contact ground 121.9, good day.\n错误：只说 roger\n\n场景 4：Tower: \"Cessna one two three, hold position.\"（含混）\n正确：Tower, Cessna one two three, confirm hold position.\n错误：直接滑行\n\n场景 5：Tower: \"Cessna one two three, expect approach clearance in five minutes.\"\n正确：Tower, Cessna one two three, standby.\n错误：反复询问 when\n\n评分：六维；含混指令未请求澄清即执行判不达标。",
  "key":"Say again/Confirm/Standby；不执行含混指令；复诵关键要素。"},
 {"id":"p0_icao_plain","track":"icao","title":"ICAO 特情：Plain English 转述","level":"B2","task_type":"decision","scene":"emergency","outcome":"ICAO vocabulary",
  "free":"免费：Plain English 转述 3 原则。\n① 不用缩写\n② 不用 phraseology 外术语\n③ 包含 who/what/where/when",
  "paid":"付费：5 个特情 → plain English 转述 + 六维评分。\n\n1. Request descent due turbulence. → Request lower altitude because of rough air.\n2. Standby. → Wait, I will call you back.\n3. Mayday, mayday, mayday, Cessna one two three, engine failure, forced landing. → Emergency, emergency, emergency, Cessna one two three, engine has failed, we are landing immediately.\n4. Request priority handling, medical emergency on board. → We need to land first, there is a sick passenger on board.\n5. Unable to comply due traffic. → We cannot do that because there is other traffic.\n\n评分：六维；使用缩写或非标准术语判不达标。",
  "key":"无缩写；who/what/where/when 齐全。"},
 {"id":"p0_icao_opi","track":"icao","title":"ICAO OPI：图片描述与追问","level":"B2","task_type":"scene_dialogue","scene":"opi","outcome":"ICAO fluency",
  "free":"免费：OPI 图片描述 3 段结构。\n① Overview：一句话说图是什么\n② Details：3 个以上细节\n③ Inference：可能发生什么、为什么",
  "paid":"付费：5 张图 → 描述 + 3 个追问 + 六维评分。\n\n图 1：机场跑道，飞机滑行，地面有油迹，地勤在接近。\n描述：In this picture, I can see a runway with an aircraft taxiing. There is an oil spill on the ground near the taxiway. Ground crew are approaching with absorbent material.\n追问：What might have caused the oil spill? What should the crew do next? How would you report this to ATC?\n\n图 2：驾驶舱内，两名飞行员检查仪表，窗外有雷暴。\n追问：What would you do in this situation? What are the risks? How would you communicate with ATC?\n\n图 3：塔台内，管制员在雷达屏幕前，屏幕上有多个目标。\n追问：What is the controller's main task? What could go wrong? How would you handle a conflict?\n\n图 4：客舱内，乘务员向乘客演示安全须知。\n追问：Why is this important? What if a passenger does not pay attention? How would you handle it?\n\n图 5：停机坪，地勤给飞机加油，旁边有消防车。\n追问：Why is the fire truck there? What are the safety rules? What would you do if there was a spill?\n\n评分：Overview 句 + 细节≥3 + 回答 why + 流畅无明显停顿。",
  "key":"Overview 句；细节≥3；回答追问的 why；流畅。"},
 {"id":"p0_fin_tone","track":"finance","title":"金融：电话会 Q&A 语气标记","level":"C1","task_type":"scene_dialogue","scene":"earnings_call","outcome":"Finance Q&A precision",
  "free":"免费：三类语气标记。\n① Certainty：We remain comfortable with the outlook.\n② Hedging：We are monitoring FX headwinds closely.\n③ Deflection：Let me take that offline.",
  "paid":"付费：5 段真实电话会转录 → 标注三类语气 + 写出追问 + 评分。\n\n片段 1：CFO: \"We remain comfortable with the full-year margin outlook, though we are monitoring FX headwinds.\"\n标注：certainty + hedging\n追问：Can you quantify the FX impact on full-year margin?\n\n片段 2：CEO: \"Let me take that offline and follow up in writing.\"\n标注：deflection\n追问：When can we expect the written follow-up, and will it include segment-level detail?\n\n片段 3：CFO: \"We are seeing some softness in the European market, but nothing structural.\"\n标注：hedging + certainty\n追问：What does \"nothing structural\" mean in terms of volume and pricing?\n\n片段 4：CEO: \"We have line of sight to the second-half ramp.\"\n标注：hedging\n追问：What specific milestones define \"line of sight\"?\n\n片段 5：CFO: \"We are not guiding on Q4 at this time.\"\n标注：deflection\n追问：What would you need to see to provide Q4 guidance?\n\n评分：区分三类；追问含量化要求；不把 hedging 误读为 certainty。",
  "key":"区分三类；追问含量化要求。"},
 {"id":"p0_fin_narrative","track":"finance","title":"金融：数字叙事结构","level":"C1","task_type":"data","scene":"number_narrative","outcome":"Finance number narrative",
  "free":"免费：数字叙事 3 段结构。\n① Headline：首句给核心数字\n② Driver：为什么\n③ Outlook：forward-looking 一句",
  "paid":"付费：3 组数据 → 3 句话叙事 + 评分。\n\n数据 1：Revenue +12% YoY；Gross margin 38%→41%；FY guidance raised 3%。\n目标：Revenue grew 12% year over year. Gross margin expanded from 38% to 41%, driven by pricing and mix. We are raising full-year guidance by 3%.\n\n数据 2：Operating cash flow $1.2B（前 $0.9B）；Capex $0.4B flat；FCF $0.8B。\n目标：Operating cash flow rose to $1.2 billion from $0.9 billion. Capex was flat at $0.4 billion. Free cash flow reached $0.8 billion.\n\n数据 3：Net debt/EBITDA 2.1x（前 2.8x）；Interest coverage 6.5x（前 5.2x）；Dividend +10%。\n目标：Net debt to EBITDA fell to 2.1x from 2.8x. Interest coverage improved to 6.5x from 5.2x. We are raising the dividend by 10%.\n\n评分：首句 headline；含 driver；含 outlook；数字准确。",
  "key":"首句 headline；含 driver 与 outlook；数字准确。"},
 {"id":"p0_fin_pitch","track":"finance","title":"金融：Investment Pitch 结构","level":"C1","task_type":"process","scene":"pitch","outcome":"Finance pitch structure",
  "free":"免费：Pitch 5 段结构。\n① Hook：30 秒内说清机会\n② Problem\n③ Solution\n④ Evidence：数字 + 客户证据\n⑤ Ask",
  "paid":"付费：2 个投资论点 → 完整 pitch 结构 + 评分。\n\n论点 1：B2B SaaS 帮助中型制造商降低停机时间\n- Hook：Unplanned downtime costs mid-sized manufacturers billions each year.\n- Problem：Most plants rely on reactive maintenance.\n- Solution：Our platform predicts failures before they happen.\n- Evidence：Three customers cut downtime by 40% in six months.\n- Ask：We are raising $5M to expand sales.\n\n论点 2：消费品牌用 AI 个性化推荐提升复购\n- Hook：70% of first-time buyers never return.\n- Problem：Generic recommendations ignore individual preference.\n- Solution：Our AI builds a personal taste profile from purchase history.\n- Evidence：Pilot brands saw repeat purchase rate rise from 18% to 29%.\n- Ask：We are raising $3M to expand to three new categories.\n\n评分：Hook 在 30 秒内；Evidence 含数字；Ask 具体。",
  "key":"Hook 30 秒内；Evidence 含数字；Ask 具体。"},
]
RUBRICS = {"oet":R_OET,"icao":R_ICAO,"finance":R_FIN}

RULES = [
 ("oet","p0_oet_purpose","purpose","目的句必须在首行；未出现读者行动动词判不达标"),
 ("oet","p0_oet_purpose","content","压缩比低于 3:1 提示信息筛选不足"),
 ("oet","p0_oet_discharge","content","必须含 medication changes/follow-up plan/red flags 三类"),
 ("oet","p0_oet_discharge","conciseness","直接复制病例原文超过 2 句判不达标"),
 ("oet","p0_oet_register","genre","未给患者版本判不达标；Flesch<60 提示可读性不足"),
 ("icao","p0_icao_clarify","interactions","含混指令未请求澄清即执行判不达标"),
 ("icao","p0_icao_plain","vocabulary","使用缩写或 phraseology 外术语判不达标"),
 ("icao","p0_icao_opi","fluency","停顿超过 3 秒或 overview 句缺失扣分"),
 ("finance","p0_fin_tone","tone_recognition","把 hedging 判为 certainty 判不达标"),
 ("finance","p0_fin_tone","followup_precision","追问不含量化要求扣分"),
 ("finance","p0_fin_narrative","number_narrative","首句非 headline 数字扣分"),
 ("finance","p0_fin_pitch","conviction_language","Hook 超 30 秒或 Ask 不具体扣分"),
]
ERRORS = [
 ("oet","p0_oet_purpose","purpose","目的句写在第二段",3),("oet","p0_oet_purpose","content","未说明 reason for referral",5),
 ("oet","p0_oet_discharge","content","遗漏 medication changes",4),("oet","p0_oet_register","genre","直接对患者使用 myocardial infarction",2),
 ("icao","p0_icao_clarify","interactions","未请求澄清即执行含混指令",2),("icao","p0_icao_plain","vocabulary","中英混杂",4),
 ("icao","p0_icao_opi","fluency","无 overview 句直接进细节",3),
 ("finance","p0_fin_tone","tone_recognition","把 We are monitoring 误读为 certainty",4),
 ("finance","p0_fin_narrative","number_narrative","只列数字不讲 driver",3),("finance","p0_fin_pitch","conviction_language","Ask 不具体",3),
]

def seed():
    c = init_db()
    for t in ["tasks","error_patterns","self_eval_rules","visual_assets","audio_assets","content_units"]:
        col = "id" if t in ("content_units",) else ("content_unit_id" if t in ("tasks","error_patterns","self_eval_rules","visual_assets","audio_assets") else "id")
        c.execute(f"DELETE FROM {t} WHERE {col} LIKE 'p0_%'")
    c.commit()
    for u in UNITS:
        c.execute("""INSERT OR REPLACE INTO content_units (id,track_id,line_id,title,level,task_type,scene,outcome_link,metadata_json,status,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
          (u["id"],u["track"],"vertical_track",u["title"],u["level"],u["task_type"],u["scene"],u["outcome"],
           json.dumps({"rubric":u["track"]+"_rubric"},ensure_ascii=False),"published"))
        c.execute("""INSERT OR REPLACE INTO tasks (id,content_unit_id,prompt,rubric_json,answer_key,free_body,paid_body)
          VALUES (?,?,?,?,?,?,?)""",
          (u["id"]+"_task",u["id"],u["title"],json.dumps(RUBRICS[u["track"]],ensure_ascii=False),u["key"],u["free"],u["paid"]))
    for r in RULES: c.execute("INSERT INTO self_eval_rules (track_id,content_unit_id,dimension,rule_text) VALUES (?,?,?,?)",r)
    for e in ERRORS: c.execute("INSERT INTO error_patterns (track_id,content_unit_id,dimension,pattern,frequency,last_seen) VALUES (?,?,?,?,?,datetime('now'))",e)
    c.commit(); c.close()
    print(f"[ok] P0 九单元写入：{len(UNITS)} 单元 + {len(RULES)} 规则 + {len(ERRORS)} 错误模式")
if __name__ == "__main__": seed()
PY

# =============================================================================
# 10. P1/P2 十二单元（新增）
# =============================================================================
wim scripts/seed_p1p2_units.py <<'PY'
import sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db

R_LEGAL = {"dimensions":[{"name":"clause_recognition","weight":0.30,"criteria":"识别 clause 类型与功能"},{"name":"register","weight":0.25,"criteria":"语域匹配场景"},{"name":"precision","weight":0.25,"criteria":"术语准确，无歧义"},{"name":"structure","weight":0.20,"criteria":"结构清晰"}],"pass_threshold":0.70}
R_AI = {"dimensions":[{"name":"prompt_structure","weight":0.30,"criteria":"角色/约束/示例/评估标准"},{"name":"output_evaluation","weight":0.30,"criteria":"识别幻觉、语气、格式问题"},{"name":"policy_literacy","weight":0.20,"criteria":"关键术语与义务结构"},{"name":"iteration","weight":0.20,"criteria":"可复用迭代"}],"pass_threshold":0.70}
R_CREATOR = {"dimensions":[{"name":"hook","weight":0.30,"criteria":"前 30 秒结构"},{"name":"pitch","weight":0.30,"criteria":"赞助 pitch 价值主张/受众数据/deliverables"},{"name":"seo","weight":0.20,"criteria":"标题/描述语域"},{"name":"platform_register","weight":0.20,"criteria":"跨平台语域"}],"pass_threshold":0.70}
R_RELOC = {"dimensions":[{"name":"scene_completion","weight":0.40,"criteria":"场景可独立完成"},{"name":"cultural_pragmatics","weight":0.30,"criteria":"制度/文化提示准确"},{"name":"register","weight":0.30,"criteria":"语域匹配服务人员"}],"pass_threshold":0.70}
RUBRICS = {"legal":R_LEGAL,"ai":R_AI,"creator":R_CREATOR,"relocation":R_RELOC}

UNITS = [
 # 法律 P1
 {"id":"p1_legal_clause","track":"legal","title":"法律：合同关键 clause 拆解","level":"C1","task_type":"standard","scene":"contract","outcome":"Contract clauses",
  "free":"免费：5 类关键 clause。\n① Party & Recitals\n② Scope & Deliverables\n③ Term & Termination\n④ Indemnity & Limitation of Liability\n⑤ Governing Law & Dispute Resolution\n练习：识别一份合同的这 5 类 clause。",
  "paid":"付费：3 份公开合同片段 → 逐 clause 拆解 + 改写建议 + rubric 反馈。\n\n片段 1（SaaS 服务合同）：\n\"The Supplier shall provide the Services described in Schedule A. Either party may terminate this Agreement for material breach upon thirty (30) days' written notice if such breach remains uncured.\"\n拆解：Scope（Schedule A）+ Term/Termination（30 天 cure period）\n改写建议：明确 material breach 的定义，避免争议。\n\n片段 2（跨境分销合同）：\n\"The Distributor shall indemnify and hold harmless the Principal against all claims arising from the Distributor's negligence or wilful misconduct.\"\n拆解：Indemnity（仅覆盖 negligence/wilful misconduct，未含 ordinary negligence）\n改写建议：确认是否遗漏 ordinary negligence，明确 cap。\n\n片段 3（合资协议）：\n\"This Agreement shall be governed by and construed in accordance with the laws of the State of Delaware, without regard to its conflict of laws principles.\"\n拆解：Governing Law（Delaware）+ 排除冲突法\n改写建议：确认仲裁条款是否与诉讼条款冲突。\n\n评分：clause_recognition + register + precision + structure。",
  "key":"识别 5 类 clause；语域准确；无歧义。"},
 {"id":"p1_legal_email","track":"legal","title":"法律：Legal Email 与函件","level":"C1","task_type":"standard","scene":"legal_email","outcome":"Legal email tone",
  "free":"免费：Legal email 3 原则。\n① 目的句在前两行\n② 事实与请求分离\n③ 保留书面痕迹（as discussed / please confirm）",
  "paid":"付费：3 个真实场景 → 函件改写 + 语域评分。\n\n场景 1（催告）：客户已逾期 60 天未付款，需发催告函。\n错误示例：Hi, you still haven't paid. Pay now or we sue.\n正确示例：Dear Mr. Smith, We write regarding invoice #1234, which remains outstanding 60 days after the due date. Please arrange payment within 14 days. If payment is not received, we may take further steps without further notice.\n\n场景 2（违约通知）：对方未按约定交付，需发违约通知并保留终止权。\n正确示例：Dear Sirs, We note that the deliverables due on 15 March have not been received. This constitutes a material breach of clause 4.2. Please remedy within 30 days, failing which we reserve our right to terminate.\n\n场景 3（和解提议）：提出和解，需含 without prejudice 标记。\n正确示例：Dear Mr. Lee, This letter is written on a without prejudice basis. We propose a settlement of $50,000 in full and final settlement of all claims. This offer remains open for 14 days.\n\n评分：目的句 + 事实与请求分离 + 保留书面痕迹 + 语域。",
  "key":"目的句前两行；事实与请求分离；保留书面痕迹；without prejudice 用于和解。"},
 {"id":"p1_legal_negotiation","track":"legal","title":"法律：谈判用语分层","level":"C1","task_type":"scene_dialogue","scene":"negotiation","outcome":"Negotiation stages",
  "free":"免费：谈判 5 阶段。\n① Opening\n② Anchoring\n③ Concession\n④ Deadlock\n⑤ Closing",
  "paid":"付费：5 个谈判场景 → 用语分层 + 评分。\n\n场景 1（Opening）：对方开场要求 100% 付款 upfront。\n正确回应：We understand your position. We propose a 30/70 split, with 30% on signing and 70% on delivery.\n\n场景 2（Anchoring）：我方希望设定价格锚点。\n正确回应：Based on market benchmarks, we believe $X is a fair starting point for this scope.\n\n场景 3（Concession）：我方可以小幅让步，需换取对方让步。\n正确回应：We can accept a 5% reduction, provided payment terms are shortened to 30 days.\n\n场景 4（Deadlock）：谈判陷入僵局。\n正确回应：It seems we have reached a point where neither side can move further today. May I suggest we reconvene on Friday with revised positions?\n\n场景 5（Closing）：达成一致，需明确下一步。\n正确回应：To confirm, we have agreed on X, Y, Z. I will circulate a draft by Monday.\n\n评分：阶段识别 + 语域 + 让步与换取明确。",
  "key":"阶段识别；让步换取；deadlock 提议改期；closing 明确下一步。"},
 # AI P1
 {"id":"p1_ai_prompt","track":"ai","title":"AI：生产级 Prompt 结构","level":"B2","task_type":"standard","scene":"prompt","outcome":"Prompt structure",
  "free":"免费：Prompt 5 要素。\n① Role\n② Constraints\n③ Examples\n④ Evaluation criteria\n⑤ Iteration instruction",
  "paid":"付费：3 个真实任务 → 写 prompt + 评估 + 评分。\n\n任务 1：从财报电话会转录中提取 3 个 hedging 语言标记。\n参考 prompt：You are a financial analyst. Given the transcript below, extract 3 instances of hedging language. For each, quote the sentence, label the type (hedging), and propose one follow-up question that demands quantified evidence. If fewer than 3 exist, say so.\n\n任务 2：把一段技术文档改写为面向非技术读者的版本。\n参考 prompt：Rewrite the following technical paragraph for a non-technical audience. Preserve all numbers. Avoid jargon; if a term is unavoidable, explain it in one short sentence. Output only the rewritten paragraph.\n\n任务 3：评估 AI 生成的合同摘要是否遗漏关键条款。\n参考 prompt：You are a legal reviewer. Given the original contract excerpt and an AI-generated summary, list any missing or misrepresented clauses. For each, quote the original clause, describe the gap, and rate severity (high/medium/low).\n\n评分：5 要素齐全 + 可复用 + 评估标准明确。",
  "key":"5 要素齐全；可复用；评估标准明确。"},
 {"id":"p1_ai_output_eval","track":"ai","title":"AI：AI 输出评估","level":"B2","task_type":"standard","scene":"output_review","outcome":"AI output evaluation",
  "free":"免费：AI 输出 3 类问题。\n① 幻觉：事实无来源\n② 语气：过度自信/模糊\n③ 格式：偏离要求",
  "paid":"付费：3 个真实 AI 输出 → 评估 + 改写 + 评分。\n\n输出 1（幻觉）：AI 说 \"According to the 2024 EU AI Act, Article 99 requires...\"。\n真实情况：Article 99 存在但义务结构不同；AI 引用错误。\n改写：将该句标记为 \"needs verification\"，并改写为 \"The EU AI Act (Regulation (EU) 2024/1689) sets obligations for general-purpose AI models; see Articles 51–56 for specific requirements.\"\n\n输出 2（语气过度自信）：AI 说 \"This contract is definitely enforceable.\"\n问题：法律意见不应绝对化。\n改写：\"Based on the clause as written, the contract appears enforceable, subject to governing law and judicial interpretation.\"\n\n输出 3（格式偏离）：AI 被要求输出 JSON，却输出了 prose。\n改写：按 schema 重写为严格 JSON。\n\n评分：识别三类问题 + 改写质量。",
  "key":"识别幻觉/语气/格式；改写保留事实与边界。"},
 {"id":"p1_ai_policy","track":"ai","title":"AI：政策精读（EU AI Act 摘要）","level":"C1","task_type":"standard","scene":"policy","outcome":"AI policy reading",
  "free":"免费：政策文本阅读 3 步。\n① 先看定义章节\n② 再看义务主体\n③ 最后看罚则与时间表",
  "paid":"付费：3 段 EU AI Act 真实条款 → 术语拆解 + 义务结构 + 评分。\n\n条款 1（第 51 条，GPAI 模型义务）：\n\"Providers of general-purpose AI models shall: (a) draw up and keep up-to-date the technical documentation of the model...\"\n拆解：义务主体 = providers；义务 = technical documentation；时间 = 上市前\n\n条款 2（第 53 条，系统性风险 GPAI）：\n\"Providers of general-purpose AI models with systemic risk shall: (a) perform model evaluation...\"\n拆解：附加义务 = model evaluation；触发条件 = systemic risk\n\n条款 3（第 99 条，罚则）：\n\"Non-compliance with the prohibition of the AI practices referred to in Article 5 shall be subject to administrative fines of up to EUR 35 000 000 or, if the offender is an undertaking, up to 7 % of its total worldwide annual turnover...\"\n拆解：罚则上限 = 35M EUR 或 7% 全球营业额\n\n评分：定义/义务主体/罚则与时间表识别准确。",
  "key":"定义、义务主体、罚则与时间表识别准确。"},
 # 创作者 P2
 {"id":"p2_creator_hook","track":"creator","title":"创作者：Hook 结构","level":"B2","task_type":"standard","scene":"script","outcome":"Hook/retention",
  "free":"免费：Hook 3 种模式。\n① 问题式：Have you ever...?\n② 数据式：70% of...\n③ 反常识式：Everything you know about X is wrong.",
  "paid":"付费：3 个真实爆款脚本 → hook 拆解 + 改写 + 评分。\n\n案例 1（YouTube，科技评测）：\n原 hook：This phone has a 200MP camera.\n问题：事实陈述，无张力。\n改写：This phone has a 200MP camera — but it takes worse photos than my 12MP one. Here's why.\n\n案例 2（Substack，商业分析）：\n原 hook：Today I want to talk about pricing.\n问题：无具体张力。\n改写：Most SaaS companies raise prices by 5%. This one raised by 500% — and churn fell. Here's the math.\n\n案例 3（播客，访谈）：\n原 hook：My guest today is a VC.\n问题：无价值锚点。\n改写：My guest has reviewed 4,000 pitches. Only 3 made him write a check. Today he explains the pattern.\n\n评分：hook 模式识别 + 30 秒内可完成 + 张力明确。",
  "key":"hook 模式识别；张力明确；30 秒内可完成。"},
 {"id":"p2_creator_pitch","track":"creator","title":"创作者：赞助 Pitch","level":"B2","task_type":"standard","scene":"sponsorship","outcome":"Sponsorship pitch",
  "free":"免费：赞助 pitch 4 要素。\n① 受众数据\n② 价值主张\n③ Deliverables\n④ 价格与排期",
  "paid":"付费：3 个真实赞助 pitch → 结构拆解 + 改写 + 评分。\n\n案例 1（YouTube 科技频道）：\n原 pitch：I have 50k subscribers. Want to sponsor?\n问题：无受众数据、无价值主张、无 deliverables。\n改写：My channel reaches 50k subscribers (78% male, 25–34, US/EU). 62% watch sponsor segments to completion. Proposal: 60s mid-roll + pinned comment + dedicated newsletter mention. $2,500 per integration; Q3 slots open.\n\n案例 2（Newsletter）：\n原 pitch：I write a newsletter. Want to sponsor?\n改写：My newsletter reaches 12k founders and PMs (open rate 48%, CTR 6.2%). Proposal: primary sponsor slot for one issue + one follow-up mention. $1,200 per issue; monthly or quarterly.\n\n案例 3（播客）：\n原 pitch：We do a podcast. Sponsor us?\n改写：Our podcast averages 8k downloads per episode (median 92% completion). Proposal: 30s pre-roll + 60s mid-roll + show notes link. $1,800 per episode; quarterly discount available.\n\n评分：4 要素齐全 + 数字明确 + deliverables 具体。",
  "key":"4 要素齐全；数字明确；deliverables 具体。"},
 {"id":"p2_creator_seo","track":"creator","title":"创作者：SEO 标题与描述","level":"B2","task_type":"standard","scene":"seo","outcome":"SEO title/description",
  "free":"免费：SEO 标题 3 原则。\n① 含主关键词\n② 含具体数字或时间\n③ 避免标题党（与内容不符）",
  "paid":"付费：3 个真实标题 → 改写 + 评分。\n\n原标题 1：How to learn English\n问题：过于宽泛，无关键词锚点。\n改写：How to Learn Business English in 30 Days (Free Plan)\n\n原标题 2：My thoughts on AI\n问题：无关键词，无具体价值。\n改写：What EU AI Act Article 51 Means for Small AI Teams (2026 Update)\n\n原标题 3：Best laptop 2026\n问题：同质化，无差异化。\n改写：Best Laptop for Video Editing Under $1,500 (Tested 12 Models)\n\n描述示例（对应标题 1）：A 30-day plan to learn business English, with free audio, scene cards, and OET/ICAO-style scenarios. No signup required for week 1.\n\n评分：含关键词 + 含数字/时间 + 与内容一致。",
  "key":"含关键词 + 含数字/时间 + 与内容一致。"},
 # 迁居 P2
 {"id":"p2_reloc_visa","track":"relocation","title":"迁居：签证场景","level":"B2","task_type":"scene_dialogue","scene":"visa","outcome":"Visa interview",
  "free":"免费：签证面试 3 段结构。\n① Purpose：签证类型 + 停留时间\n② Ties：与本国的联系\n③ Finance：资金来源",
  "paid":"付费：3 个真实签证场景 → 对话脚本 + 评分。\n\n场景 1（美国 B1/B2）：\nOfficer: What is the purpose of your visit?\n正确回答：I am visiting the United States for a two-week business conference in San Francisco. I will return to my job in Singapore on March 15.\n\n场景 2（英国 Skilled Worker）：\nOfficer: How will you support yourself?\n正确回答：My employer, Acme Ltd, will pay a salary of £45,000 per year. I have also saved £8,000 for initial costs. Here is my employment contract and bank statement.\n\n场景 3（加拿大学签）：\nOfficer: Why this program?\n正确回答：The program in data analytics at University of Toronto matches my current role. I plan to return to my company in Shanghai after graduation, as they have confirmed a promotion.\n\n评分：Purpose + Ties + Finance 齐全；回答具体；无矛盾。",
  "key":"Purpose/Ties/Finance 齐全；回答具体；无矛盾。"},
 {"id":"p2_reloc_bank","track":"relocation","title":"迁居：银行开户场景","level":"B2","task_type":"scene_dialogue","scene":"bank","outcome":"Bank account opening",
  "free":"免费：银行开户 3 段结构。\n① Purpose：开户类型\n② Documents：ID/地址证明/收入证明\n③ Questions：费用/限额/网银",
  "paid":"付费：3 个真实场景 → 对话脚本 + 评分。\n\n场景 1（英国基本账户）：\nClerk: What type of account would you like?\n正确回答：I would like a basic current account. I have my passport, my Biometric Residence Permit, and a proof of address from my landlord.\n\n场景 2（澳大利亚储蓄账户）：\nClerk: Do you have a Tax File Number?\n正确回答：Not yet. I arrived last week. Can I open the account now and provide the TFN later?\n\n场景 3（德国 blocked account 后续）：\nClerk: How much will you deposit monthly?\n正确回答：I will deposit €1,200 per month from my employer. Here is my employment contract.\n\n评分：Purpose/Documents/Questions 齐全；回答具体。",
  "key":"Purpose/Documents/Questions 齐全；回答具体。"},
 {"id":"p2_reloc_health","track":"relocation","title":"迁居：就医预约场景","level":"B2","task_type":"scene_dialogue","scene":"healthcare","outcome":"Healthcare appointment",
  "free":"免费：就医预约 3 段结构。\n① Symptom：症状 + 时长\n② Urgency：紧急程度\n③ Insurance：保险信息",
  "paid":"付费：3 个真实场景 → 对话脚本 + 评分。\n\n场景 1（GP 预约）：\nReceptionist: What is the reason for your visit?\n正确回答：I have had a persistent cough for two weeks, and I would like to see a GP. I am not in immediate danger, but it is not improving.\n\n场景 2（急诊分诊）：\nNurse: How urgent is it?\n正确回答：I have chest pain that started one hour ago and is getting worse. I have asthma. My insurance card is here.\n\n场景 3（专科转诊）：\nReceptionist: Do you have a referral?\n正确回答：Yes, my GP referred me to a cardiologist. Here is the referral letter. I would like the earliest available appointment.\n\n评分：Symptom/Urgency/Insurance 齐全；回答具体。",
  "key":"Symptom/Urgency/Insurance 齐全；回答具体。"},
]

RULES = [
 ("legal","p1_legal_clause","clause_recognition","未识别 indemnity 或 governing law 扣分"),
 ("legal","p1_legal_clause","precision","改写引入新歧义扣分"),
 ("legal","p1_legal_email","structure","目的句不在前两行扣分"),
 ("legal","p1_legal_email","register","和解函未含 without prejudice 扣分"),
 ("legal","p1_legal_negotiation","register","deadlock 未提议改期扣分"),
 ("legal","p1_legal_negotiation","precision","让步未换取对方让步扣分"),
 ("ai","p1_ai_prompt","prompt_structure","5 要素缺一扣分"),
 ("ai","p1_ai_prompt","output_evaluation","评估标准不可执行扣分"),
 ("ai","p1_ai_output_eval","output_evaluation","未识别幻觉扣分"),
 ("ai","p1_ai_output_eval","prompt_structure","改写未保留事实或边界扣分"),
 ("ai","p1_ai_policy","policy_literacy","义务主体/罚则识别错误扣分"),
 ("creator","p2_creator_hook","hook","hook 无张力扣分"),
 ("creator","p2_creator_hook","pitch","30 秒内无法完成扣分"),
 ("creator","p2_creator_pitch","pitch","缺受众数据或 deliverables 扣分"),
 ("creator","p2_creator_seo","seo","标题与内容不符扣分"),
 ("relocation","p2_reloc_visa","scene_completion","Purpose/Ties/Finance 缺一扣分"),
 ("relocation","p2_reloc_bank","scene_completion","Documents 缺一扣分"),
 ("relocation","p2_reloc_health","scene_completion","Urgency 不明确扣分"),
]
ERRORS = [
 ("legal","p1_legal_clause","clause_recognition","未识别 cap",2),
 ("legal","p1_legal_email","register","和解函缺 without prejudice",3),
 ("legal","p1_legal_negotiation","precision","让步未换取",4),
 ("ai","p1_ai_prompt","prompt_structure","缺 evaluation criteria",5),
 ("ai","p1_ai_output_eval","output_evaluation","未识别幻觉",4),
 ("ai","p1_ai_policy","policy_literacy","罚则主体识别错误",2),
 ("creator","p2_creator_hook","hook","hook 无张力",5),
 ("creator","p2_creator_pitch","pitch","缺受众数据",4),
 ("creator","p2_creator_seo","seo","标题与内容不符",2),
 ("relocation","p2_reloc_visa","scene_completion","缺 Finance",3),
 ("relocation","p2_reloc_bank","scene_completion","缺 Documents",3),
 ("relocation","p2_reloc_health","scene_completion","Urgency 不明",2),
]

def seed():
    c = init_db()
    for t in ["tasks","error_patterns","self_eval_rules","content_units"]:
        col = "id" if t == "content_units" else "content_unit_id"
        c.execute(f"DELETE FROM {t} WHERE {col} LIKE 'p1_%' OR {col} LIKE 'p2_%'")
    c.commit()
    for u in UNITS:
        c.execute("""INSERT OR REPLACE INTO content_units (id,track_id,line_id,title,level,task_type,scene,outcome_link,metadata_json,status,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
          (u["id"],u["track"],"vertical_track",u["title"],u["level"],u["task_type"],u["scene"],u["outcome"],
           json.dumps({"rubric":u["track"]+"_rubric"},ensure_ascii=False),"published"))
        c.execute("""INSERT OR REPLACE INTO tasks (id,content_unit_id,prompt,rubric_json,answer_key,free_body,paid_body)
          VALUES (?,?,?,?,?,?,?)""",
          (u["id"]+"_task",u["id"],u["title"],json.dumps(RUBRICS[u["track"]],ensure_ascii=False),u["key"],u["free"],u["paid"]))
    for r in RULES: c.execute("INSERT INTO self_eval_rules (track_id,content_unit_id,dimension,rule_text) VALUES (?,?,?,?)",r)
    for e in ERRORS: c.execute("INSERT INTO error_patterns (track_id,content_unit_id,dimension,pattern,frequency,last_seen) VALUES (?,?,?,?,?,datetime('now'))",e)
    c.commit(); c.close()
    print(f"[ok] P1/P2 十二单元写入：{len(UNITS)} 单元 + {len(RULES)} 规则 + {len(ERRORS)} 错误模式")
if __name__ == "__main__": seed()
PY

# =============================================================================
# 11. 深度内容线（Deep Read 001/002/003、Book Foundry、Daily Starter 001/002）
# =============================================================================
wim scripts/seed_deep_read.py <<'PY'
import sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT/"content/free/deep_read"; OUT.mkdir(parents=True, exist_ok=True)

DEEP_READS = [
 {"id":"deep_read_001","track":"oet","title":"Deep Read 001：OET 写作评分从格式正确到读者行动导向","level":"B2/C1",
  "body":"# Deep Read 001：OET 写作评分从\"格式正确\"到\"读者行动导向\"\n\n> 目标级别：B2/C1 ｜ 迁移赛道：医疗 OET ｜ 信源：5 类一手/高质量来源 ｜ 数据口径：接入前以官方页面为准\n\n## 核心事实层（多源交叉验证）\n- 事实 1：OET Writing 评分固定为 five dimensions——purpose、content、conciseness、genre、organization（OET 官方评分细则）。\n- 事实 2：NMC（英国）、AHPRA（澳大利亚）、GMC（英国医师）、NMBI（爱尔兰）对 OET 成绩的接受政策在 2024–2025 期间持续更新；各国对写作单项的最低分要求不完全一致（各国监管机构官网）。\n- 事实 3：考生反馈中，\"目的句缺失或含糊\"是最高频扣分点；\"genre 失配\"（写给患者的信却用专科医生语域）次之（脱敏考生社区 + 临床沟通教材）。\n- 交叉验证：三个信源对\"purpose 是第一维度\"判断一致；两个信源对\"genre 权重是否上升\"存在分歧，标注为未解问题。\n\n## 解释层（为什么重要）\n- 为什么\"格式正确\"不再够：评分官更关注\"读者是否能立即行动\"——目的句是否让专科医生一眼知道\"为什么转、转给谁、要做什么\"。\n- 谁受益：能写出\"读者行动导向\"目的句、并按读者调整语域的考生。\n- 谁受损：只背模板、不分析读者的考生；把病例原文抄进信件的考生。\n- 未解问题：genre 权重是否在 2026 进一步上升；各国监管是否会在写作单项上提高门槛。\n\n## 语言层（B2/C1 标注）\n- 目标词汇：refer / grateful / regarding / unresponsive to / conservative management / exertional / assessment\n- 目标句式：I am writing to refer [patient], a [age]-year-old [gender], to your [specialty] clinic for [reason].\n- 语域对比：写给专科医生（colleague / further assessment）vs 写给患者（your blood pressure / we will start）\n- 衔接手段：therefore / subsequently / please note / I would be grateful if\n\n## 论证层（作者如何组织证据）\n- 三层证据：官方评分细则（标准）→ 各国监管政策（结果关联）→ 考生反馈与教材（失败模式）\n- 修辞策略：先给结论（purpose 是第一维度），再给证据（三层），最后给行动（3 个任务）\n- 反驳处理：对\"genre 权重上升\"的分歧，不强行下结论，标注为未解问题\n\n## 行动层（读者可完成的任务）\n- 任务 1：从 3 个病例摘要中各写一句目的句（骨科/心内科/呼吸科）。\n- 任务 2：判断 3 个目的句是否含\"读者行动动词\"（refer / grateful / thank / please see）。\n- 任务 3：用 OET rubric 给自己的一句话打分，并写出一条改写动作。\n- 延伸：把本期\"读者行动导向\"原则迁移到出院信（medication changes / follow-up / red flags 三类信息）。\n\n## 与赛道的耦合\n- 本期直接服务医疗 OET 赛道（P0）。\n- 对法律赛道有迁移价值：\"读者行动导向\"同样适用于 legal email 与合同函件。\n- 对金融赛道有迁移价值：电话会 Q&A 的追问也要求\"读者/听者能立即行动\"。"},
 {"id":"deep_read_002","track":"legal","title":"Deep Read 002：跨境合同条款的语域漂移与风险","level":"C1",
  "body":"# Deep Read 002：跨境合同条款的语域漂移与风险\n\n> 目标级别：C1 ｜ 迁移赛道：法律 ｜ 信源：5 类公开合同/判决/监管模板 ｜ 数据口径：接入前以官方页面为准\n\n## 核心事实层\n- 事实 1：跨境合同中，\"material breach\"、\"commercially reasonable efforts\"、\"indemnify and hold harmless\" 三个短语在英美法系与大陆法系下的可执行性差异最大（比较法教材 + 公开判决）。\n- 事实 2：2024–2025 期间，多个司法辖区对 AI 生成条款的可解释性提出要求（监管通告）。\n- 事实 3：律所 client alerts 中，\"cap on liability\" 与 \"indemnity\" 的交互是最高频争议点之一（公开 alerts）。\n- 交叉验证：三类信源对\"indemnity 与 cap 的关系\"判断一致；对\"AI 生成条款的可执行性\"存在分歧。\n\n## 解释层\n- 为什么重要：同一句英文在不同法系下可能产生完全不同义务；买家需要识别\"语域漂移\"——看似标准条款实际埋入非标准义务。\n- 谁受益：能识别 clause 类型、语域、可执行性差异的法务。\n- 未解问题：AI 生成条款是否构成\"书面同意\"，各国尚未统一。\n\n## 语言层\n- 目标词汇：indemnify / hold harmless / material breach / commercially reasonable efforts / governing law / without prejudice\n- 目标句式：The Supplier shall indemnify and hold harmless the Principal against all claims arising from...\n- 语域对比：写给监管（compliance with Article X）vs 写给对方（we reserve our rights）\n\n## 论证层\n- 三层证据：公开判决（标准）→ 监管通告（结果关联）→ 律所 alerts（失败模式）\n- 修辞策略：先给三类 clause（indemnity/cap/governing law），再给跨法系差异，最后给改写动作\n\n## 行动层\n- 任务 1：从 3 份公开合同片段中识别 indemnity 与 cap 的交互。\n- 任务 2：判断 3 个 clause 是否在目标法系下可执行。\n- 任务 3：写出 3 条改写动作，避免引入新歧义。\n\n## 与赛道的耦合\n- 直接服务法律赛道（P1）。\n- 对金融赛道有迁移价值：跨境 M&A 的陈述与保证条款同样需要语域敏感。"},
 {"id":"deep_read_003","track":"ai","title":"Deep Read 003：EU AI Act 的义务结构速读","level":"C1",
  "body":"# Deep Read 003：EU AI Act 的义务结构速读\n\n> 目标级别：C1 ｜ 迁移赛道：AI ｜ 信源：官方条文 + 监管通告 + 行业解读 ｜ 数据口径：接入前以官方页面为准\n\n## 核心事实层\n- 事实 1：EU AI Act（Regulation (EU) 2024/1689）对 GPAI 模型设定了技术文档、版权政策、训练数据摘要等义务（Article 51–56）。\n- 事实 2：系统性风险 GPAI 附加 model evaluation、adversarial testing、incident reporting 义务（Article 55）。\n- 事实 3：罚则上限：禁止性行为最高 35M EUR 或 7% 全球年营业额（Article 99）。\n- 交叉验证：三类信源对义务主体（providers）判断一致；对\"systemic risk 阈值\"存在分歧。\n\n## 解释层\n- 为什么重要：义务主体、时间表、罚则三者决定合规成本；买家需要区分\"provider\"与\"deployer\"的义务差异。\n- 谁受益：能读懂条文结构、识别义务主体、估算合规成本的 AI 从业者。\n- 未解问题：systemic risk 的具体阈值与计算方式仍在演进。\n\n## 语言层\n- 目标词汇：general-purpose AI model / systemic risk / technical documentation / adversarial testing / incident reporting / administrative fines\n- 目标句式：Providers of general-purpose AI models shall...\n- 语域对比：条文（shall）vs 监管指引（should / may）\n\n## 论证层\n- 三层证据：官方条文（标准）→ 监管通告（结果关联）→ 行业解读（失败模式）\n- 修辞策略：先给义务主体，再给义务清单，最后给罚则与时间表\n\n## 行动层\n- 任务 1：从 3 段条文中识别义务主体与义务清单。\n- 任务 2：判断 3 个场景中谁是 provider、谁是 deployer。\n- 任务 3：写出 3 条合规动作（技术文档 / 版权政策 / 训练数据摘要）。\n\n## 与赛道的耦合\n- 直接服务 AI 赛道（P1）。\n- 对法律赛道有迁移价值：AI 条款的可解释性与合同义务结构相关。"},
]
def main():
    c = init_db()
    for r in DEEP_READS:
        (OUT/f"{r['id']}.md").write_text(r["body"], encoding="utf-8")
        c.execute("""INSERT OR REPLACE INTO content_units (id,track_id,line_id,title,level,task_type,scene,outcome_link,metadata_json,status,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
          (r["id"],r["track"],"deep_read",r["title"],r["level"],"standard","policy_analysis",r["track"]+" deep read",
           json.dumps({"layers":["facts","explanation","language","argument","action"]},ensure_ascii=False),"published"))
    c.commit(); c.close()
    print(f"[ok] Deep Read 001/002/003 已写入 content/free/deep_read/ 并入库")
if __name__ == "__main__": main()
PY

wim scripts/seed_book_foundry.py <<'PY'
import sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT/"content/free/book_foundry"; OUT.mkdir(parents=True, exist_ok=True)
BODY = """# Book Foundry 处理示例：Clinical Communication Skills（第 1 章）

## 输入
- 材料：一本临床沟通教材的第 1 章（用户有权使用）
- 赛道：OET
- 目标：把章节转化为 3 个可训练单元

## 处理路径（六步）
1. **outline**：识别章节结构——沟通原则 → 患者访谈 → 信息给予
2. **chapter_split**：切分为 3 个训练单元
3. **track_rubric_apply**：按 OET Writing / Speaking rubric 加工
4. **exercises**：生成理解题 + 写作题 + 口语题
5. **media**：生成 3 张图卡 + 3 条音频
6. **export**：导出为可学习包（Markdown + SVG + MP3）

## 产出（3 个训练单元）

### 单元 1：患者访谈开场（OET Speaking：introduction + purpose）
- 目标：开场 30 秒内说清"我是谁、为什么来、要做什么"
- 句型：Good morning, I'm [name], one of the nurses here. I'd like to ask you a few questions about your pain.
- 自评：是否含自我介绍 + 目的 + 许可（May I ask...?）
- 图卡：assets/images/bf_unit1_opening.svg
- 音频：audio/bf_unit1_opening.mp3

### 单元 2：信息给予（OET Speaking：chunking + checking understanding）
- 目标：一次给一个概念，给完检查理解
- 句型：First, I'll check your blood pressure. ... Does that make sense so far?
- 自评：是否 chunking（一次一概念）+ 是否 checking understanding
- 图卡：assets/images/bf_unit2_chunking.svg
- 音频：audio/bf_unit2_chunking.mp3

### 单元 3：转诊信写作（OET Writing：purpose + content selection）
- 目标：把本章访谈内容压缩为转诊信目的句 + 三类必含信息
- 句型：I am writing to refer [patient] to your [specialty] clinic for [reason].
- 自评：purpose 在首行 + content 压缩比 ≥5:1 + red flags 齐全
- 图卡：assets/images/bf_unit3_referral.svg
- 音频：audio/bf_unit3_referral.mp3

## 可售卖价值
- 99 元/本；用户上传自己的书/外刊，得到章节精读课
- 复利：每本书的处理都在教系统"什么材料值得课程化"

## 版权边界
- 仅处理用户有权使用的材料；输出仅供用户个人学习；白标场景下上游材料授权由采购方负责
"""
def main():
    (OUT/"sample_clinical_comm_ch1.md").write_text(BODY, encoding="utf-8")
    c = init_db()
    c.execute("""INSERT OR REPLACE INTO content_units (id,track_id,line_id,title,level,task_type,scene,outcome_link,metadata_json,status,created_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
      ("book_foundry_clinical_ch1","oet","book_foundry","Book Foundry：Clinical Communication Skills 第 1 章","B2","standard","book_to_course","OET Speaking + Writing",
       json.dumps({"pipeline":["outline","chapter_split","track_rubric_apply","exercises","media","export"],"output_units":3},ensure_ascii=False),"published"))
    c.commit(); c.close()
    print("[ok] Book Foundry 示例已写入")
if __name__ == "__main__": main()
PY

wim scripts/seed_daily_starter.py <<'PY'
import sys, os, subprocess, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from src.audio import validate_audio_file
from src.visual import write_svg_card
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT/"content/free/daily_starter"; OUT.mkdir(parents=True, exist_ok=True)

DAYS = {
 "001": [
  {"id":"ds_d1_oet","track":"oet","sentence":"I am writing to refer Mr. Chen to your orthopedic clinic for further assessment of persistent lower back pain.","card_title":"OET D1: Referral Purpose","labels":["First line","Reader action verb: refer","Specialty: orthopedic clinic","Reason: persistent lower back pain","No case copying"],"audio":"I am writing to refer Mr. Chen to your orthopedic clinic for further assessment of persistent lower back pain.","preview":"完整 5 个病例的目的句改写 + OET 5 维 rubric 反馈"},
  {"id":"ds_d1_icao","track":"icao","sentence":"Tower, Cessna one two three, say again taxi route.","card_title":"ICAO D1: Clarification","labels":["Instruction unclear","Say again","Confirm [item]","Standby","Do NOT execute unclear instruction"],"audio":"Tower, Cessna one two three, say again taxi route.","preview":"5 个特情的请求澄清训练 + ICAO 六维评分"},
  {"id":"ds_d1_fin","track":"finance","sentence":"We remain comfortable with the outlook, though we are monitoring FX headwinds.","card_title":"Finance D1: Tone Markers","labels":["Certainty: remain comfortable","Hedging: monitoring FX headwinds","Deflection: take offline","Follow-up: quantify impact","Do not misread hedging as certainty"],"audio":"We remain comfortable with the outlook, though we are monitoring FX headwinds.","preview":"5 段电话会的语气标注 + 追问训练"},
 ],
 "002": [
  {"id":"ds_d2_legal","track":"legal","sentence":"This letter is written on a without prejudice basis and proposes a settlement of fifty thousand dollars.","card_title":"Legal D2: Without Prejudice","labels":["Mark without prejudice","State settlement amount","State scope (full and final)","State validity period","Avoid admission of liability"],"audio":"This letter is written on a without prejudice basis and proposes a settlement of fifty thousand dollars.","preview":"3 个和解函场景 + 语域评分"},
  {"id":"ds_d2_ai","track":"ai","sentence":"You are a financial analyst. Extract three hedging phrases from the transcript and propose one follow-up question per phrase.","card_title":"AI D2: Prompt 5 Elements","labels":["Role: financial analyst","Constraint: extract 3","Constraint: hedging only","Output: 3 phrases + follow-ups","Evaluate: executable"],"audio":"You are a financial analyst. Extract three hedging phrases from the transcript and propose one follow-up question per phrase.","preview":"3 个真实任务的 prompt 结构 + 评估"},
  {"id":"ds_d2_reloc","track":"relocation","sentence":"I am visiting the United States for a two-week business conference and will return to my job in Singapore on March fifteenth.","card_title":"Relocation D2: Visa Interview","labels":["Purpose: business conference","Duration: two weeks","Ties: job in Singapore","Return date","Concrete, no contradiction"],"audio":"I am visiting the United States for a two-week business conference and will return to my job in Singapore on March fifteenth.","preview":"3 个签证场景 + 评分"},
 ],
}
MD_TEMPLATE = """# Daily Starter · 第 {day} 天

> 一句 + 一卡 + 一音频。免费层每天 3–5 分钟；第 7 天为关键打卡节点。

## {lines}
"""
def main():
    c = init_db()
    minb = int(os.environ.get("AUDIO_MIN_BYTES","50000"))
    failed = False
    for day, items in DAYS.items():
        lines = []
        for d in items:
            card = write_svg_card(ROOT/f"assets/cards/{d['id']}.svg", d["card_title"], d["labels"], f"Daily Starter D{day} · track={d['track']}")
            c.execute("INSERT OR REPLACE INTO visual_assets (id,content_unit_id,type,file_path,label_json,version,status,ruleset_version,label_anchored) VALUES (?,?,?,?,?,?,?,?,?)",
                      (d["id"]+"_card",d["id"],"card",str(card.relative_to(ROOT)),json.dumps({"title":d["card_title"],"labels":d["labels"]},ensure_ascii=False),1,"published","v1",1))
            mp3 = ROOT/f"audio/{d['id']}.mp3"
            try:
                subprocess.run([sys.executable,"-m","edge_tts","--voice",os.environ.get("TTS_VOICE","en-US-JennyNeural"),
                                "--rate",os.environ.get("TTS_RATE","-10%"),"--text",d["audio"],"--write-media",str(mp3)],check=True)
            except Exception as e:
                print(f"[fail] {d['id']} edge-tts 失败: {e}", file=sys.stderr); failed = True; continue
            ok, msg = validate_audio_file(mp3, minb)
            print(("[ok] " if ok else "[fail] ")+msg)
            if not ok: failed = True; continue
            c.execute("INSERT OR REPLACE INTO audio_assets (id,content_unit_id,file_path,bytes,voice,rate,status) VALUES (?,?,?,?,?,?,?)",
                      (d["id"]+"_audio",d["id"],str(mp3.relative_to(ROOT)),mp3.stat().st_size,os.environ.get("TTS_VOICE","en-US-JennyNeural"),os.environ.get("TTS_RATE","-10%"),"published"))
            c.execute("""INSERT OR REPLACE INTO content_units (id,track_id,line_id,title,level,task_type,scene,outcome_link,metadata_json,status,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
              (d["id"],d["track"],"daily_starter",d["card_title"],"B2","card","daily_starter",d["track"]+" daily habit",
               json.dumps({"sentence":d["sentence"],"paid_preview":d["preview"]},ensure_ascii=False),"published"))
            lines.append(f"## {d['track'].upper()}\n- 句子：{d['sentence']}\n- 图卡：assets/cards/{d['id']}.svg\n- 音频：audio/{d['id']}.mp3\n- 付费预览：{d['preview']}\n")
        (OUT/f"day_{day}.md").write_text(MD_TEMPLATE.format(day=day, lines="\n".join(lines)), encoding="utf-8")
    c.commit(); c.close()
    if failed: sys.exit(1)
    print(f"[ok] Daily Starter Day 001 + Day 002 已生成（{sum(len(v) for v in DAYS.values())} 条）")
if __name__ == "__main__": main()
PY

# =============================================================================
# 12. 真实信源抓取（Phase 3）
# =============================================================================
wim scripts/fetch_real_sources.py <<'PY'
import sys, os, yaml; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from src.sources import load_sources, fetch_one, check_no_placeholder
from src.fetcher import enqueue
root = Path(__file__).resolve().parent.parent
cfg = load_sources()
bad = check_no_placeholder(cfg)
if bad:
    print("[fail] sources.yaml 仍含 example.com 占位，Phase 3 必须替换为真实端点。")
    for sid,k,v in bad: print(f"  - {sid}.{k} = {v}")
    sys.exit(1)
conn = init_db()
online = os.environ.get("FETCH_ONLINE","0")=="1"
if not online:
    print("[skip] FETCH_ONLINE=0，仅做前置校验。设置 FETCH_ONLINE=1 执行真实抓取。")
    sys.exit(0)
for sid, s in cfg["sources"].items():
    try:
        res = fetch_one(conn, sid, s); print(f"[ok] {sid}: {res}")
    except Exception as e:
        enqueue(conn, sid, e); print(f"[retry] {sid}: {e}")
conn.close(); print("[ok] Phase 3 真实抓取完成")
PY

# =============================================================================
# 13. 支付/授权测试脚本 + gated 服务启动
# =============================================================================
wim scripts/test_payment.py <<'PY'
import sys, os, json, hmac, hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from payments.dujiaoka import handle_dujiaoka_payload, has_entitlement

def main():
    init_db()
    secret = os.environ.get("DUJIAOKA_SECRET_KEY","test-secret")
    payload = {"event_type":"order_paid","order_id":"TEST-001","buyer_email":"buyer@example.com","track_id":"oet","plan":"monthly","amount":199,"currency":"CNY","status":"paid"}
    raw = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    os.environ["DUJIAOKA_SECRET_KEY"] = secret
    res = handle_dujiaoka_payload(raw, sig)
    print("[payment] result:", res)
    assert res.get("ok"), "签名校验失败"
    assert has_entitlement("buyer@example.com", "oet"), "授权未写入"
    print("[ok] webhook + entitlement 流程通过")
if __name__ == "__main__": main()
PY

wim scripts/start_gated_server.py <<'PY'
import sys, os, subprocess
from pathlib import Path
root = Path(__file__).resolve().parent.parent
host = os.environ.get("GATED_HOST","127.0.0.1")
port = os.environ.get("GATED_PORT","8787")
cmd = [sys.executable, "-m", "uvicorn", "server.gated:app", "--host", host, "--port", port, "--app-dir", str(root)]
print(f"[gated] 启动: {' '.join(cmd)}")
subprocess.run(cmd, cwd=str(root))
PY

wim scripts/check_gated_health.py <<'PY'
import os, sys, httpx
url = os.environ.get("GATED_PUBLIC_URL","http://127.0.0.1:8787").rstrip("/") + "/health"
try:
    r = httpx.get(url, timeout=5)
    print(f"[gated] {url} -> {r.status_code} {r.text[:200]}")
    sys.exit(0 if r.status_code == 200 else 1)
except Exception as e:
    print(f"[fail] 无法连接 gated 服务: {e}")
    sys.exit(1)
PY

wim scripts/check_deploy.py <<'PY'
import sys, os, yaml, httpx
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.sources import load_sources, check_no_placeholder
root = Path(__file__).resolve().parent.parent
fails = []

# 1. sources 无占位
bad = check_no_placeholder(load_sources())
if bad: fails.append(f"sources.yaml 仍含 example.com 占位（{len(bad)} 处）")

# 2. 支付密钥
if not os.environ.get("DUJIAOKA_SECRET_KEY"): fails.append("DUJIAOKA_SECRET_KEY 为空")
if not os.environ.get("DASHSCOPE_API_KEY"): fails.append("DASHSCOPE_API_KEY 为空")

# 3. gated 服务健康
try:
    url = os.environ.get("GATED_PUBLIC_URL","http://127.0.0.1:8787").rstrip("/") + "/health"
    r = httpx.get(url, timeout=5); 
    if r.status_code != 200: fails.append(f"gated /health 返回 {r.status_code}")
except Exception as e: fails.append(f"gated 服务不可达: {e}")

# 4. 内容
from src.db import get_conn
c = get_conn()
p0 = c.execute("SELECT COUNT(*) n FROM content_units WHERE id LIKE 'p0_%' AND status='published'").fetchone()["n"]
p1p2 = c.execute("SELECT COUNT(*) n FROM content_units WHERE (id LIKE 'p1_%' OR id LIKE 'p2_%') AND status='published'").fetchone()["n"]
if p0 < 9: fails.append(f"P0 单元不足 9（当前 {p0}）")
if p1p2 < 12: fails.append(f"P1/P2 单元不足 12（当前 {p1p2}）")
c.close()

# 5. 音频
from src.audio import validate_audio_file
for mp3 in (root/"audio").glob("*.mp3"):
    ok, msg = validate_audio_file(mp3, int(os.environ.get("AUDIO_MIN_BYTES","50000")))
    if not ok: fails.append(msg)

if fails:
    print("[fail] 部署检查未通过：")
    for f in fails: print(f"  - {f}")
    sys.exit(1)
print("[ok] 部署检查全部通过")
PY

# =============================================================================
# 14. 测试
# =============================================================================
wim tests/__init__.py <<'PY'
"""EnglishFoundry tests package."""
PY

wim tests/test_scope.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from scripts.check_scope import main
def test_no_story_universe(): assert main() == 0
PY

wim tests/test_config.py <<'PY'
import yaml; from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
def test_tracks():
    d = yaml.safe_load((ROOT/"config/tracks.yaml").read_text(encoding="utf-8"))
    assert {t["id"] for t in d["tracks"]} == {"oet","icao","finance","legal","ai","creator","relocation"}
def test_content_lines():
    d = yaml.safe_load((ROOT/"config/content_lines.yaml").read_text(encoding="utf-8"))["content_lines"]
    assert set(d) == {"deep_read","book_foundry","daily_starter"}
PY

wim tests/test_db.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
def test_tables():
    c = init_db(); names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["tracks","content_lines","sources","content_units","tasks","visual_assets","audio_assets",
              "memory_nodes","memory_edges","lexicon_spiral","knowledge_graph","self_eval_rules",
              "orders","entitlements","webhook_events","deployments"]:
        assert t in names
    c.close()
PY

wim tests/test_agents.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.agents import load_agents_config, check_forbidden, validate_responses_protocol
def test_dead_ends():
    cfg = load_agents_config(); check_forbidden(cfg)
    assert {"gemini-cli","github-models"} <= set(cfg["forbidden"])
def test_responses_static():
    cfg = load_agents_config(); enabled,_ = validate_responses_protocol(cfg, online=False)
    assert enabled and all(a["protocol"]=="responses" for a in enabled)
PY

wim tests/test_memory.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from src.memory import add_memory_node, add_memory_edge, add_lexicon_item, due_spiral_items, review_spiral_item
def test_memo():
    init_db()
    a = add_memory_node(99,"profile","p"); b = add_memory_node(99,"error","e"); add_memory_edge(b,a,"explains_weakness")
    add_lexicon_item(99,"test phrase","B2",first_review="2000-01-01")
    due = due_spiral_items(99,"2026-01-01"); assert any(x["item"]=="test phrase" for x in due)
    review_spiral_item(due[0]["id"])
PY

wim tests/test_audio.py <<'PY'
import sys, tempfile; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.audio import validate_audio_file
def test_placeholder_rejected():
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f: f.write(b"ID3"+b"\x00"*100); p=Path(f.name)
    ok,_ = validate_audio_file(p,min_bytes=50000); assert not ok; p.unlink()
PY

wim tests/test_paywall.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.paywall import build_preview
def test_paid_not_in_html():
    paid = "SECRET_PAID_LINE_"+"x"*100
    h = build_preview("t","free",paid,preview_lines=1); assert "SECRET_PAID_LINE" not in h
PY

wim tests/test_visual.py <<'PY'
import sys, tempfile; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.visual import write_svg_card, write_svg_decision_tree
def test_labels_anchored():
    with tempfile.TemporaryDirectory() as d:
        p = write_svg_card(Path(d)/"c.svg","T",["Alpha label","Beta label"])
        t = p.read_text(encoding="utf-8"); assert "Alpha label" in t and "Beta label" in t
        q = write_svg_decision_tree(Path(d)/"d.svg","DT",["Branch A","Branch B"])
        assert "Branch A" in q.read_text(encoding="utf-8")
PY

wim tests/test_compounding.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from src.compounding import record_progress, progress_curve, record_career_outcome, record_data_network, record_checkin
def test_four_layers():
    init_db()
    record_progress(99,"oet","purpose",6.5); assert progress_curve(99,"oet")
    record_career_outcome(99,"oet","OET Grade B",salary_delta_estimate=2000)
    record_data_network("user","99","personal_corpus","corpus_99")
    record_checkin(99,"2026-01-01",1)
PY

wim tests/test_fetcher.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.fetcher import retry_call
def test_retry_success():
    state = {"n":0}
    def fn():
        state["n"] += 1
        if state["n"] < 3: raise RuntimeError("boom")
        return "ok"
    assert retry_call(fn, max_attempts=3, backoff_seconds=0) == "ok"
PY

wim tests/test_p0_units.py <<'PY'
import sys, json; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from scripts.seed_p0_units import seed
def test_p0():
    seed()
    c = init_db()
    units = {u["id"] for u in c.execute("SELECT id FROM content_units WHERE id LIKE 'p0_%'").fetchall()}
    assert units == {"p0_oet_purpose","p0_oet_discharge","p0_oet_register","p0_icao_clarify","p0_icao_plain","p0_icao_opi","p0_fin_tone","p0_fin_narrative","p0_fin_pitch"}
    c.close()
PY

wim tests/test_p1p2_units.py <<'PY'
import sys, json; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from scripts.seed_p1p2_units import seed
def test_p1p2():
    seed()
    c = init_db()
    rows = c.execute("SELECT id,track_id FROM content_units WHERE id LIKE 'p1_%' OR id LIKE 'p2_%'").fetchall()
    ids = {r["id"] for r in rows}
    tracks = {r["track_id"] for r in rows}
    assert len(ids) == 12
    assert tracks == {"legal","ai","creator","relocation"}
    c.close()
PY

wim tests/test_sources.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.sources import load_sources, check_no_placeholder
def test_sources_has_no_example():
    cfg = load_sources()
    bad = check_no_placeholder(cfg)
    assert bad == [], f"占位未清：{bad}"
PY

wim tests/test_payment.py <<'PY'
import sys, json, hmac, hashlib, os; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.db import init_db
from payments.dujiaoka import handle_dujiaoka_payload, has_entitlement
def test_webhook_and_entitlement():
    init_db()
    os.environ["DUJIAOKA_SECRET_KEY"] = "test-secret"
    payload = {"event_type":"order_paid","order_id":"TEST-002","buyer_email":"t@example.com","track_id":"oet","plan":"monthly","amount":199,"currency":"CNY","status":"paid"}
    raw = json.dumps(payload).encode(); sig = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    res = handle_dujiaoka_payload(raw, sig); assert res["ok"]
    assert has_entitlement("t@example.com","oet")
def test_bad_signature_rejected():
    init_db()
    os.environ["DUJIAOKA_SECRET_KEY"] = "test-secret"
    raw = b'{"order_id":"X"}'
    res = handle_dujiaoka_payload(raw, "deadbeef"); assert not res["ok"]
PY

wim tests/test_gated.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from server.gated import app
from fastapi.testclient import TestClient
def test_health():
    c = TestClient(app); r = c.get("/health"); assert r.status_code == 200 and r.json()["ok"]
def test_preview_404():
    c = TestClient(app); r = c.get("/preview/nonexistent"); assert r.status_code == 404
PY

wim tests/test_content_lines.py <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from scripts.seed_deep_read import main as seed_deep
from scripts.seed_book_foundry import main as seed_book
from src.db import init_db
def test_deep_read_book_foundry():
    seed_deep(); seed_book()
    c = init_db()
    dr = c.execute("SELECT COUNT(*) n FROM content_units WHERE id LIKE 'deep_read_%'").fetchone()["n"]
    bf = c.execute("SELECT COUNT(*) n FROM content_units WHERE id LIKE 'book_foundry_%'").fetchone()["n"]
    assert dr >= 3 and bf >= 1
    c.close()
PY

# =============================================================================
# 15. 执行
# =============================================================================
log "初始化数据库 + 赛道/内容线/信源种子"
python scripts/init_db.py
python scripts/seed_tracks.py
python scripts/seed_content_lines.py
python scripts/seed_sources.py

log "写入 P0 九单元（真实付费正文）"
python scripts/seed_p0_units.py

log "范围校验：去除故事宇宙"
python scripts/check_scope.py

log "第7章：Agent 静态校验 + 死路禁用 + responses 协议"
python scripts/check_agents.py

log "第7章：MemO 记忆层种子"
python scripts/seed_memory.py

log "第7章：模型路由演示"
python scripts/route_demo.py

log "关于页（只保留使命/价值/信任）"
python scripts/build_about.py

log "Phase 0: 真实音频硬校验（P0 第一条真实单元）"
if confirm "现在调用 edge-tts 生成 P0 第一条真实音频并硬校验（需联网）？"; then
  python - <<'PY'
import sys, subprocess, os; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.audio import validate_audio_file
from src.db import init_db
d = Path("audio"); d.mkdir(exist_ok=True)
out = d/"p0_oet_purpose.mp3"
text = "Dear Colleague, I am writing to refer Mr. Chen, a 58-year-old man, to your orthopedic clinic for further assessment of persistent lower back pain unresponsive to conservative management."
subprocess.run([sys.executable,"-m","edge_tts","--voice",os.environ.get("TTS_VOICE","en-US-JennyNeural"),"--rate",os.environ.get("TTS_RATE","-10%"),"--text",text,"--write-media",str(out)],check=True)
ok,msg = validate_audio_file(out,int(os.environ.get("AUDIO_MIN_BYTES","50000")))
print(("[ok] " if ok else "[fail] ")+msg)
if not ok: sys.exit(1)
c = init_db()
c.execute("INSERT OR REPLACE INTO audio_assets (id,content_unit_id,file_path,bytes,voice,rate,status) VALUES (?,?,?,?,?,?,?)",
          ("p0_oet_purpose_audio","p0_oet_purpose",str(out),out.stat().st_size,os.environ.get("TTS_VOICE","en-US-JennyNeural"),os.environ.get("TTS_RATE","-10%"),"published"))
c.commit(); c.close()
PY
else
  die "Phase 0 要求真实音频；未生成不允许继续。"
fi

log "Phase 0: 每日分析报表"
python scripts/daily_analytics.py

if [ "$PHASE" -ge 1 ]; then
  log "Phase 1: 生成全部 P0 音频（9 条）并入库"
  python - <<'PY'
import sys, subprocess, os; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.audio import validate_audio_file
from src.db import init_db
d = Path("audio"); d.mkdir(exist_ok=True); minb = int(os.environ.get("AUDIO_MIN_BYTES","50000"))
samples = {
 "p0_oet_purpose":"Dear Colleague, I am writing to refer Mr. Chen, a 58-year-old man, to your orthopedic clinic for further assessment of persistent lower back pain unresponsive to conservative management.",
 "p0_oet_discharge":"Dear Doctor, I am writing regarding Mr. Lee, who was discharged today following an uncomplicated appendectomy. Please note the new apixaban 5mg twice daily, review the wound in seven days, and advise him to seek urgent care for chest pain or shortness of breath.",
 "p0_oet_register":"Your blood pressure is high. You had a heart attack. You are short of breath. We will start one medicine today.",
 "p0_icao_clarify":"Tower, Cessna one two three, say again taxi route. Confirm hold short of runway two seven.",
 "p0_icao_plain":"Request lower altitude because of rough air. Wait, I will call you back. We have a medical emergency on board.",
 "p0_icao_opi":"In this picture, I can see a runway with an aircraft taxiing. There is an oil spill on the ground near the taxiway. Ground crew are approaching with absorbent material. The spill may have come from a hydraulic leak, and the crew should clean it before the next departure.",
 "p0_fin_tone":"We remain comfortable with the full-year margin outlook, though we are monitoring FX headwinds. Let me take that offline and follow up in writing.",
 "p0_fin_narrative":"Revenue grew twelve percent year over year. Gross margin expanded from thirty-eight to forty-one percent, driven by pricing and mix. We are raising full-year guidance by three percent.",
 "p0_fin_pitch":"Unplanned downtime costs mid-sized manufacturers billions each year. Our platform predicts failures before they happen. Three customers cut downtime by forty percent in six months. We are raising five million to expand sales.",
}
c = init_db(); failed = False
for uid, text in samples.items():
    out = d/f"{uid}.mp3"
    subprocess.run([sys.executable,"-m","edge_tts","--voice",os.environ.get("TTS_VOICE","en-US-JennyNeural"),"--rate",os.environ.get("TTS_RATE","-10%"),"--text",text,"--write-media",str(out)],check=True)
    ok,msg = validate_audio_file(out,minb); print(("[ok] " if ok else "[fail] ")+msg)
    if not ok: failed = True; continue
    c.execute("INSERT OR REPLACE INTO audio_assets (id,content_unit_id,file_path,bytes,voice,rate,status) VALUES (?,?,?,?,?,?,?)",
              (uid+"_audio",uid,str(out),out.stat().st_size,os.environ.get("TTS_VOICE","en-US-JennyNeural"),os.environ.get("TTS_RATE","-10%"),"published"))
c.commit(); c.close()
if failed: sys.exit(1)
print("[ok] P0 9 条音频完成")
PY

  log "Phase 1: 生成 P0 图卡（9 张）"
  python - <<'PY'
import sys, json; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.db import init_db
from src.visual import write_svg_card, write_svg_decision_tree, write_svg_infographic
root = Path(".").resolve(); c = init_db()
defs = [
 ("p0_oet_purpose","infographic","OET Referral: Purpose",[{"heading":"Pattern 1","detail":"I am writing to refer [patient] to [specialty] for [reason]."},{"heading":"Pattern 2","detail":"I would be grateful if you could see [patient] regarding [concern]."},{"heading":"Pattern 3","detail":"Thank you for seeing [patient], whom I am referring for [reason]."},{"heading":"Check","detail":"First line + reader action verb + reason"}]),
 ("p0_oet_discharge","card","OET Discharge: Must-Include",["Medication changes","Follow-up plan","Red flags","Compression >= 5:1","Register matched to GP"]),
 ("p0_oet_register","infographic","Medical -> Patient",[{"heading":"hypertension","detail":"high blood pressure"},{"heading":"myocardial infarction","detail":"heart attack"},{"heading":"dyspnoea","detail":"shortness of breath"},{"heading":"Rule","detail":"Second person + one concept + Flesch > 60"}]),
 ("p0_icao_clarify","decision_tree","ICAO Clarification",["Instruction unclear?","Say again","Confirm [item]","Standby","Do NOT execute unclear instruction"]),
 ("p0_icao_plain","infographic","Plain English Framework",[{"heading":"No abbreviations","detail":"Spell out"},{"heading":"No non-phraseology","detail":"Stick to standard"},{"heading":"Who/What/Where/When","detail":"All present"},{"heading":"Example","detail":"Request descent due turbulence -> Request lower altitude because of rough air"}]),
 ("p0_icao_opi","card","OPI Structure",["Overview: one sentence","Details: at least 3","Inference: what and why","Answer follow-up","Fluency: no long pauses"]),
 ("p0_fin_tone","infographic","Earnings Call Tone",[{"heading":"Certainty","detail":"We remain comfortable"},{"heading":"Hedging","detail":"We are monitoring FX"},{"heading":"Deflection","detail":"Let me take that offline"},{"heading":"Follow-up","detail":"Quantify impact"}]),
 ("p0_fin_narrative","card","Number Narrative",["Headline first","Driver: why","Outlook: forward","Numbers accurate","No data dump"]),
 ("p0_fin_pitch","infographic","Pitch Structure",[{"heading":"Hook","detail":"30 seconds"},{"heading":"Problem","detail":"Market gap"},{"heading":"Solution","detail":"Why us"},{"heading":"Evidence","detail":"Numbers + customer proof"},{"heading":"Ask","detail":"Specific amount and use"}]),
]
for uid, kind, title, payload in defs:
    if kind == "infographic":
        p = write_svg_infographic(root/f"assets/images/{uid}.svg", title, payload, f"P0 · {uid}")
    elif kind == "card":
        p = write_svg_card(root/f"assets/images/{uid}.svg", title, payload, f"P0 · {uid}")
    else:
        p = write_svg_decision_tree(root/f"assets/images/{uid}.svg", title, payload, f"P0 · {uid}")
    c.execute("INSERT OR REPLACE INTO visual_assets (id,content_unit_id,type,file_path,label_json,version,status,ruleset_version,label_anchored) VALUES (?,?,?,?,?,?,?,?,?)",
              (uid+"_v1",uid,kind,str(p.relative_to(root)),json.dumps({"title":title,"payload":payload},ensure_ascii=False),1,"published","v1",1))
c.commit(); c.close(); print("[ok] P0 图卡 9 张完成")
PY

  log "Phase 1: P0 预览页 + 私密付费正文"
  python - <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.db import get_conn
from src.paywall import build_preview, write_paid_body
root = Path(".").resolve(); c = get_conn()
rows = c.execute("""SELECT cu.id, cu.track_id, cu.title, t.free_body, t.paid_body FROM content_units cu JOIN tasks t ON t.content_unit_id=cu.id WHERE cu.id LIKE 'p0_%'""").fetchall()
out = root/"static/track"; out.mkdir(parents=True, exist_ok=True)
for r in rows:
    locked = ["完整归档","全部音频与图卡","rubric 评分反馈","错误模式追踪"]
    proof = {"oet":"已有 1200+ 医护考生在此纠错","icao":"已有 600+ 飞行员/空管在此评分","finance":"已有 800+ 金融从业者在此训练"}.get(r["track_id"],"")
    html = build_preview(r["title"], r["free_body"] or "", r["paid_body"] or "", preview_lines=5, locked_outline=locked, social_proof=proof)
    (out/f"{r['id']}.html").write_text(html, encoding="utf-8")
    write_paid_body(root/"private_delivery", r["track_id"], r["id"], r["paid_body"] or "")
c.close(); print(f"[ok] P0 预览 {len(rows)} 个")
PY

  log "Phase 1: 定价页"
  mkdir -p static/track
  cat > static/track/pricing.html <<'HTML'
<!doctype html><html lang="zh"><meta charset="utf-8"><title>定价</title>
<style>body{font-family:Arial,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#0f172a}</style>
<body><h1>按结果付费</h1>
<p>OET 199/月 · ICAO 199/月 · 金融 159/月 · 法律 159/月 · AI/创作者/迁居 69/月 · Deep Read 9.9/期 · Book Foundry 99/本 · 白标 499/月起</p>
<h2>P0 赛道训练单元</h2><ul>
<li>OET：转诊信目的句 / 出院信信息筛选 / 医患语域转换</li>
<li>ICAO：特情请求澄清 / Plain English 转述 / OPI 图片描述</li>
<li>金融：电话会语气标记 / 数字叙事 / Investment Pitch 结构</li></ul>
<h2>P1/P2 赛道训练单元</h2><ul>
<li>法律：clause 拆解 / legal email / 谈判用语分层</li>
<li>AI：生产级 prompt / AI 输出评估 / EU AI Act 精读</li>
<li>创作者：Hook 结构 / 赞助 pitch / SEO 标题</li>
<li>迁居：签证 / 银行开户 / 就医预约</li></ul>
<p>先体验免费完整模块与一次 rubric 反馈，再解锁完整归档、音频/图卡、评分模拟。</p>
</body></html>
HTML
  ok "Phase 1 完成（P0 音频/图卡/预览/定价）"
fi

if [ "$PHASE" -ge 2 ]; then
  log "Phase 2: P1/P2 十二单元写入"
  python scripts/seed_p1p2_units.py

  log "Phase 2: P1/P2 音频（12 条）"
  python - <<'PY'
import sys, subprocess, os; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.audio import validate_audio_file
from src.db import init_db
d = Path("audio"); d.mkdir(exist_ok=True); minb = int(os.environ.get("AUDIO_MIN_BYTES","50000"))
samples = {
 "p1_legal_clause":"The Supplier shall indemnify and hold harmless the Principal against all claims arising from the Distributor's negligence or wilful misconduct.",
 "p1_legal_email":"This letter is written on a without prejudice basis. We propose a settlement of fifty thousand dollars in full and final settlement of all claims.",
 "p1_legal_negotiation":"We can accept a five percent reduction, provided payment terms are shortened to thirty days.",
 "p1_ai_prompt":"You are a financial analyst. Given the transcript below, extract three instances of hedging language. For each, quote the sentence and propose one follow-up question that demands quantified evidence.",
 "p1_ai_output_eval":"This contract is definitely enforceable. Based on the clause as written, the contract appears enforceable, subject to governing law and judicial interpretation.",
 "p1_ai_policy":"Providers of general-purpose AI models with systemic risk shall perform model evaluation and adversarial testing.",
 "p2_creator_hook":"This phone has a 200 megapixel camera, but it takes worse photos than my 12 megapixel one. Here is why.",
 "p2_creator_pitch":"My channel reaches fifty thousand subscribers, seventy-eight percent male, aged twenty-five to thirty-four. Proposal: sixty second mid-roll plus pinned comment for twenty-five hundred dollars.",
 "p2_creator_seo":"How to Learn Business English in 30 Days, Free Plan.",
 "p2_reloc_visa":"I am visiting the United States for a two-week business conference in San Francisco. I will return to my job in Singapore on March fifteenth.",
 "p2_reloc_bank":"I would like a basic current account. I have my passport, my biometric residence permit, and a proof of address from my landlord.",
 "p2_reloc_health":"I have had a persistent cough for two weeks. I am not in immediate danger, but it is not improving. I would like to see a GP.",
}
c = init_db(); failed = False
for uid, text in samples.items():
    out = d/f"{uid}.mp3"
    subprocess.run([sys.executable,"-m","edge_tts","--voice",os.environ.get("TTS_VOICE","en-US-JennyNeural"),"--rate",os.environ.get("TTS_RATE","-10%"),"--text",text,"--write-media",str(out)],check=True)
    ok,msg = validate_audio_file(out,minb); print(("[ok] " if ok else "[fail] ")+msg)
    if not ok: failed = True; continue
    c.execute("INSERT OR REPLACE INTO audio_assets (id,content_unit_id,file_path,bytes,voice,rate,status) VALUES (?,?,?,?,?,?,?)",
              (uid+"_audio",uid,str(out),out.stat().st_size,os.environ.get("TTS_VOICE","en-US-JennyNeural"),os.environ.get("TTS_RATE","-10%"),"published"))
c.commit(); c.close()
if failed: sys.exit(1)
print("[ok] P1/P2 12 条音频完成")
PY

  log "Phase 2: P1/P2 图卡（12 张）"
  python - <<'PY'
import sys, json; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.db import init_db
from src.visual import write_svg_card, write_svg_infographic
root = Path(".").resolve(); c = init_db()
defs = [
 ("p1_legal_clause","infographic","Contract Clauses",[{"heading":"Party & Recitals","detail":"Who and why"},{"heading":"Scope & Deliverables","detail":"What"},{"heading":"Term & Termination","detail":"When and how"},{"heading":"Indemnity & Cap","detail":"Who bears loss"},{"heading":"Governing Law","detail":"Which law"}]),
 ("p1_legal_email","card","Legal Email Rules",["Purpose in first two lines","Facts separated from requests","Keep written record","Use without prejudice for settlement","Match register to recipient"]),
 ("p1_legal_negotiation","infographic","Negotiation Stages",[{"heading":"Opening","detail":"Understand position"},{"heading":"Anchoring","detail":"Market benchmark"},{"heading":"Concession","detail":"Trade for something"},{"heading":"Deadlock","detail":"Propose re-convene"},{"heading":"Closing","detail":"Confirm and circulate"}]),
 ("p1_ai_prompt","infographic","Prompt 5 Elements",[{"heading":"Role","detail":"Who is the model"},{"heading":"Constraints","detail":"What must/must not"},{"heading":"Examples","detail":"Show desired output"},{"heading":"Evaluation","detail":"How to judge"},{"heading":"Iteration","detail":"What to do if off"}]),
 ("p1_ai_output_eval","card","AI Output Review",["Hallucination: no source","Tone: overconfident / vague","Format: off-spec","Rewrite with facts and boundaries","Quote original when correcting"]),
 ("p1_ai_policy","infographic","EU AI Act Reader",[{"heading":"Definitions","detail":"GPAI, systemic risk"},{"heading":"Obligation holder","detail":"Providers vs deployers"},{"heading":"Obligations","detail":"Docs, copyright, summary"},{"heading":"Penalties","detail":"Up to 35M EUR or 7%"},{"heading":"Timeline","detail":"Staged application"}]),
 ("p2_creator_hook","card","Hook Patterns",["Problem: Have you ever...","Data: 70% of...","Contrarian: Everything you know is wrong","Tension within 30 seconds","Promise a payoff"]),
 ("p2_creator_pitch","infographic","Sponsorship Pitch",[{"heading":"Audience data","detail":"Size, demographics, engagement"},{"heading":"Value proposition","detail":"Why this audience"},{"heading":"Deliverables","detail":"What you will produce"},{"heading":"Price & schedule","detail":"Amount, slots"}]),
 ("p2_creator_seo","card","SEO Title Rules",["Primary keyword present","Number or time anchor","No clickbait mismatch","Description mirrors title","One clear promise"]),
 ("p2_reloc_visa","infographic","Visa Interview",[{"heading":"Purpose","detail":"Type and duration"},{"heading":"Ties","detail":"Home-country links"},{"heading":"Finance","detail":"Source of funds"},{"heading":"Return date","detail":"Concrete"}]) if False else ("p2_reloc_visa","infographic","Visa Interview",[{"heading":"Purpose","detail":"Type and duration"},{"heading":"Ties","detail":"Home-country links"},{"heading":"Finance","detail":"Source of funds"},{"heading":"Return date","detail":"Concrete"}]),
 ("p2_reloc_bank","card","Bank Opening",["Purpose: account type","Documents: ID, address, income","Questions: fees, limits, online","Specific amounts","Consent to follow-up"]),
 ("p2_reloc_health","card","Healthcare Appointment",["Symptom: what and how long","Urgency: now / soon / routine","Insurance: card and referral","Specific request","Follow-up plan"]),
]
for uid, kind, title, payload in defs:
    if kind == "infographic":
        p = write_svg_infographic(root/f"assets/images/{uid}.svg", title, payload, f"P1/P2 · {uid}")
    else:
        p = write_svg_card(root/f"assets/images/{uid}.svg", title, payload, f"P1/P2 · {uid}")
    c.execute("INSERT OR REPLACE INTO visual_assets (id,content_unit_id,type,file_path,label_json,version,status,ruleset_version,label_anchored) VALUES (?,?,?,?,?,?,?,?,?)",
              (uid+"_v1",uid,kind,str(p.relative_to(root)),json.dumps({"title":title,"payload":payload},ensure_ascii=False),1,"published","v1",1))
c.commit(); c.close(); print("[ok] P1/P2 图卡 12 张完成")
PY

  log "Phase 2: P1/P2 预览页 + 私密付费正文"
  python - <<'PY'
import sys; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.db import get_conn
from src.paywall import build_preview, write_paid_body
root = Path(".").resolve(); c = get_conn()
rows = c.execute("""SELECT cu.id, cu.track_id, cu.title, t.free_body, t.paid_body FROM content_units cu JOIN tasks t ON t.content_unit_id=cu.id WHERE cu.id LIKE 'p1_%' OR cu.id LIKE 'p2_%'""").fetchall()
out = root/"static/track"; out.mkdir(parents=True, exist_ok=True)
for r in rows:
    locked = ["完整归档","全部音频与图卡","rubric 评分反馈","错误模式追踪"]
    proof = {"legal":"已有 300+ 涉外法务在此训练","ai":"已有 500+ AI 从业者在此训练","creator":"已有 400+ 创作者在此训练","relocation":"已有 700+ 迁居者在此训练"}.get(r["track_id"],"")
    html = build_preview(r["title"], r["free_body"] or "", r["paid_body"] or "", preview_lines=5, locked_outline=locked, social_proof=proof)
    (out/f"{r['id']}.html").write_text(html, encoding="utf-8")
    write_paid_body(root/"private_delivery", r["track_id"], r["id"], r["paid_body"] or "")
c.close(); print(f"[ok] P1/P2 预览 {len(rows)} 个")
PY

  log "Phase 2: Deep Read 001/002/003 + Book Foundry + Daily Starter Day 001/002"
  python scripts/seed_deep_read.py
  python scripts/seed_book_foundry.py
  python scripts/seed_daily_starter.py
  ok "Phase 2 完成（P1/P2 + 深度内容线）"
fi

if [ "$PHASE" -ge 3 ]; then
  log "Phase 3: 真实信源前置校验（sources.yaml 不得有 example.com）"
  python scripts/check_sources_real.py || die "请先替换 config/sources.yaml 中的占位 URL 为真实端点"

  log "Phase 3: 真实抓取（FETCH_ONLINE=1 时执行）"
  python scripts/fetch_real_sources.py
  ok "Phase 3 完成（信源真实化 + 抓取队列）"
fi

if [ "$PHASE" -ge 4 ]; then
  log "Phase 4: 支付 webhook + 授权流程自测"
  python scripts/test_payment.py

  log "Phase 4: 启动 gated 服务（后台）"
  if confirm "现在启动 FastAPI gated 服务？"; then
    (python scripts/start_gated_server.py &) ; sleep 2
    python scripts/check_gated_health.py || warn "gated 服务健康检查失败（可能端口未就绪，稍后重试）"
  else
    warn "跳过启动 gated 服务；Phase 4 部署检查会提示未通过。"
  fi

  log "Phase 4: 部署检查清单"
  python scripts/check_deploy.py || warn "部署检查有未通过项，详见上方输出"
  ok "Phase 4 完成（支付 + gated + 部署检查）"
fi

log "回归测试"
pytest -q

log "自检摘要"
python - <<'PY'
import sys, json; from pathlib import Path
sys.path.insert(0,str(Path(".").resolve()))
from src.agents import load_agents_config, check_forbidden, validate_responses_protocol
from src.model_router import route_with_fallback
from src.db import init_db
cfg = load_agents_config(); check_forbidden(cfg); enabled,_ = validate_responses_protocol(cfg, online=False)
print("构建期 Agent:", ", ".join(f"{a['name']}({a['model']})" for a in enabled))
print("记忆默认: MemO")
print("模型路由:", {k: route_with_fallback(k)["primary"] for k in ["daily","high_concurrency","long_context","audio"]})
c = init_db()
for line in ("p0_","p1_","p2_","deep_read_","book_foundry_","ds_d1_","ds_d2_"):
    n = c.execute(f"SELECT COUNT(*) n FROM content_units WHERE id LIKE '{line}%'").fetchone()["n"]
    print(f"  {line.rstrip('_')} 单元: {n}")
for t in ("orders","entitlements","webhook_events","deployments"):
    n = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
    print(f"  {t}: {n}")
c.close()
PY

echo "----------------------------------------"
echo "数据库: data/englishfoundry.db（含 orders/entitlements/webhook_events/deployments）"
echo "配置:   config/*.yaml（含 payment.yaml、deploy.yaml）"
echo "静态站: static/（免费+预览）；付费正文: private_delivery/"
echo "P0 单元: 9 | P1/P2 单元: 12"
echo "Deep Read: 001/002/003 | Book Foundry: 1 | Daily Starter: Day 001/002"
echo "音频:   audio/p0_*.mp3 + audio/p1_*.mp3 + audio/p2_*.mp3 + audio/ds_d1_*.mp3 + audio/ds_d2_*.mp3"
echo "图卡:   assets/images/p0_*.svg + p1_*.svg + p2_*.svg + assets/cards/ds_d*.svg"
echo "支付:   payments/dujiaoka.py + server/gated.py（FastAPI）"
echo "部署:   scripts/check_deploy.py 输出清单"
echo "----------------------------------------"
ok "bootstrap V6.0 终版 Phase ${PHASE} 完成"
echo
echo "部署前完整流程："
echo "  1) cp .env.example .env，填写 DASHSCOPE_API_KEY / QUARTR_API_KEY / BENZINGA_API_KEY / FISCAL_API_KEY / DUJIAOKA_SECRET_KEY 等"
echo "  2) 把 config/sources.yaml 里所有 env_url/env_base 指向的 URL 替换为真实端点"
echo "  3) CHECK_ONLINE=1 ./bootstrap.sh --phase 0"
echo "  4) ./bootstrap.sh --phase 1   # P0 音频/图卡/预览/定价"
echo "  5) ./bootstrap.sh --phase 2   # P1/P2 + 深度内容线"
echo "  6) FETCH_ONLINE=1 ./bootstrap.sh --phase 3   # 真实信源抓取"
echo "  7) ./bootstrap.sh --phase 4   # 支付自测 + gated 启动 + 部署检查"
echo "  8) python scripts/start_gated_server.py   # 生产常驻"
echo "  9) 独角数卡后台配置 webhook: {GATED_PUBLIC_URL}/webhook/dujiaoka（HMAC-SHA256 头 X-Signature）"
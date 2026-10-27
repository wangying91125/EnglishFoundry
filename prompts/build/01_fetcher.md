任务：实现 src/foundry/fetcher.py，按 config/sources.yaml 抓取全球英文 RSS/Atom。
要求：
- 用 feedparser 解析 RSS；对没有正文的条目，用 requests+BeautifulSoup 抓取 url 正文（超时、自定义 User-Agent，失败则仅用摘要）。
- 每个源独立 try/except 并记录日志，单源失败不影响其他源。
- tenacity 指数退避重试（最多 3 次）。
- 输出 List[Article]，Article 字段见 foundry.models；published 统一为 timezone-aware datetime；id 用 url 的稳定哈希。
- 提供 fetch_all(enabled_only=True, limit_per_source=None) 与 fetch_one(source_cfg)。
不要使用任何付费或需登录的 API；不要依赖 jina.ai。
验收：`python -c "from foundry.fetcher import fetch_all; a=fetch_all(limit_per_source=2); print(len(a))"` 在能联网的环境返回 >0 条目。

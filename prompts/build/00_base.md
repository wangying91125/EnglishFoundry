任务：在当前项目（englishfoundry，全球实时英文内容工厂）建立可运行的 Python 基础。
创建：
1) pyproject.toml：定义包名 foundry，src 布局（package-dir = {"" = "src"}），依赖：feedparser、requests、beautifulsoup4、lxml、pyyaml、openai、python-dotenv、tenacity、jinja2、edge-tts；开发依赖 pytest。
2) src/foundry/__init__.py。
3) src/foundry/config.py：加载 config/*.yaml；用 dotenv 读取项目根 .env；环境变量 DASHSCOPE_API_KEY、DASHSCOPE_BASE_URL、FOUNDRY_MODEL 覆盖配置；提供 load_settings() 返回带属性的对象（含 sources 路径、llm.base_url、llm.api_key、llm.model、tts 默认口音/语速、output 目录）。
4) src/foundry/models.py：用 @dataclass 定义 Article（id, source, category, title, url, published, content, tags:list）、Story（id, title, summary, url, source, category, published, articles:list）、ContentPack（story_id, data:dict, path）。
要求：纯标准库+上述依赖；所有路径相对项目根；不写死任何 Key。
验收：`pip install -e .` 成功；`python -c "from foundry.config import load_settings; print(load_settings().llm.base_url)"` 能打印 URL。

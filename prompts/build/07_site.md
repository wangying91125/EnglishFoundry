任务：实现静态站点生成 src/foundry/site.py 与 web/ 模板。
要求：
- site.py：读取 output 内容（packs、角色 markdown、平台文案、音频路径），用 Jinja2 渲染静态站点到 web/：index.html（今日精选、按角色/类别筛选、客户端搜索）、story 详情页、role 页、about.html。
- 模板内联/放在 web/templates 或直接在 site.py 中；纯静态、无后端、无外部框架依赖（可少量原生 CSS/JS）。
- 设计：浅色主题、现代简洁、移动端响应式、英文内容为主、中文导航说明；列出音频播放器与下载入口。
- 提供 build_site(settings) 并输出生成的文件清单。
验收：build_site 对 fixture 数据生成 web/index.html 且文件非空、含关键字；写 tests/test_site.py。

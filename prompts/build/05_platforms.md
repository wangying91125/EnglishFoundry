任务：实现 6 平台渲染：config/platforms.yaml 与 src/foundry/platforms.py。
在 platforms.yaml 定义（key, name, format, title_rule, char_limit, emoji, hashtags, notes）：
xiaohongshu（小红书，钩子标题+分点+emoji，约 600 字，带话题标签）、wechat_mp（公众号，深度长文，小标题）、shortvideo（短视频口播，30-60 秒口语逐字稿，前 3 秒钩子）、weibo（微博，140 字内+#话题#）、x（X/Twitter，英文，280 字符内，1-2 个 hashtag）、linkedin（LinkedIn，英文专业，300-600 词，结尾 CTA）。
要求：platforms.py：load_platforms()；render_for_platform(role_markdown, meta, platform_key) -> 适配文案；中文平台输出中文框架+英文学习内容，英文平台输出英文。
验收：对同一角色 markdown，6 个平台均返回符合各自 char_limit/格式的文案；写 tests/test_platforms.py。

任务：实现“母版内容包”生成：src/foundry/master.py 与 prompts/master.md。
要求：
- prompts/master.md：要求模型对一篇英文新闻，输出严格 JSON，键为：
	levels（A1,A2,B1,B2,C1,C2 各自改写 title 与 body）、bilingual（逐句 {en, zh} 数组，基于 B1 版本）、vocabulary（[{word,pos,zh,en_def,example}]，12 个）、grammar（[{sentence, point, explanation_zh}]，3 条长难句）、background_zh（中文背景，150 字内）、listening_script（B1 口语化文本）、speaking（3 个讨论问题 + 1 个角色扮演场景）、writing（1 篇范文题目 + 范文 120-200 词）、quiz（[{q, choices, answer, explain}]，5 题）。
- master.py：用 OpenAI 兼容客户端（base_url/api_key/model 来自 settings.llm），chat.completions，response_format 要求 JSON；tenacity 重试；可通过 modules 参数选择生成哪些键；把结果保存为 output/packs/<story_id>.json 并返回 ContentPack。
- JSON 解析要健壮（剥离 ```json 围栏、容错）。
验收：用 monkeypatch 假的 LLM 返回（读 tests/fixtures/sample_pack.json）写 tests/test_master.py，不依赖真实网络即可通过。

任务：实现 src/foundry/normalizer.py，把 Article 清洗、去重、聚合成 Story。
要求：
- clean_html：去标签、统一空白、规整。
- 只保留英文内容（简单判断：英文字符占比）。
- 按时间过滤（默认最近 24h，可配）。
- 去重：URL 规范化 + 标题归一化（小写、去标点）完全相同去重。
- 同事件聚类：从标题提取关键词（去掉停用词），用 Jaccard 或 token 集合相似度（阈值可配，默认 0.55）合并为 Story，Story.articles 包含各来源。
- 提供 normalize(articles, settings) -> List[Story]。
验收：对构造的示例（同一事件两篇、不相关一篇）能聚成 2 个 Story；为该函数写 tests/test_normalizer.py。

任务：实现 12 角色渲染：config/roles.yaml 与 src/foundry/roles.py。
在 roles.yaml 定义 12 个角色（key, name, levels（偏好难度，可多个）、modules（从 ContentPack 取用的键）、tone、word_limit、extra）：
k12、postgrad（考研）、cet（四六级）、ielts_toefl（雅思托福）、business（商务职场）、trade（外贸）、crossborder（跨境电商）、developer（程序员/技术）、academic（学术）、creator（内容创作者/出海）、immigration（移民留学/海外生活）、kids（少儿亲子）。
要求：
- roles.py：load_roles()；render_for_role(pack:ContentPack, role_key) -> markdown 字符串，按该角色的 levels/modules/tone/word_limit 从母版选取并组装，含标题、正文、词汇、练习、答案折叠区。
- 每个角色的侧重真实合理（如 kids 只用 A1/A2 并多词汇、developer 侧重 tech 类与 B2-C1、trade 侧重商务表达）。
验收：render_for_role 对 fixture ContentPack 与至少 3 个角色返回非空 markdown 且长度受 word_limit 约束；写 tests/test_roles.py。

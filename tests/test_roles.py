"""foundry.roles 测试：离线，用 tests/fixtures/sample_pack.json 当母版，不联网、不调 LLM。

覆盖：config/roles.yaml 的 12 个角色与校验、按 levels/modules 选材、word_limit 约束下的
压缩与回填、必含骨架（标题/正文/词汇/练习/答案折叠区）、角色侧重的真实合理性，
以及配置非法时的报错和命令行入口。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from foundry.config import ENV_ROOT
from foundry.master import PACK_MODULES
from foundry.models import ContentPack
from foundry.roles import (
    DEFAULT_TRIM_ORDER,
    EXPECTED_ROLES,
    MAX_DETAIL,
    ROLES_FILE_NAME,
    SECTION_ORDER,
    RolesError,
    count_words,
    load_roles,
    render_all,
    render_for_role,
    role_for,
    roles_path,
    topic_match,
)
from foundry.roles import _main as roles_main

FIXTURE = Path(__file__).parent / "fixtures" / "sample_pack.json"
REPO_ROLES = Path(__file__).resolve().parents[1] / "config" / ROLES_FILE_NAME

#: 母版键 → 板块标题片段（用来核对“角色没声明的键不许出现”）
HEADING_MARKERS = {
    "levels": "## 📖 正文",
    "background_zh": "## 🧭 背景速览",
    "bilingual": "## 🔎 逐句精读",
    "vocabulary": "## 📚 核心词汇",
    "grammar": "## 🔧 长难句拆解",
    "listening_script": "## 🎧 听力稿",
    "speaking": "## 🗣 开口说",
    "writing": "## ✍️ 写作练习",
    "quiz": "## 📝 小练习",
}
BASE_ROLE = "name: A\n    levels: [B1]\n    modules: [levels]\n    tone: t\n    word_limit: 300\n    "


@pytest.fixture(scope="module")
def pack() -> ContentPack:
    """fixture 母版（和 output/packs/*.json 同形状）。"""
    return ContentPack.load(FIXTURE)


@pytest.fixture(scope="module")
def roles():
    """仓库里的 12 个角色。"""
    return load_roles(REPO_ROLES)


def write_roles(tmp_path: Path, text: str) -> Path:
    """在临时项目根里写一份 config/roles.yaml 并返回路径。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / ROLES_FILE_NAME
    target.write_text(text, encoding="utf-8")
    return target


def section(markdown: str, keyword: str) -> str:
    """取某个 `##` 板块的正文（按标题关键字找），没有就返回空串。"""
    for block in markdown.split("\n## ")[1:]:
        heading, _, body = block.partition("\n")
        if keyword in heading:
            return body.split("\n---\n")[0].strip()
    return ""


def table_rows(text: str) -> list[str]:
    """Markdown 表格的数据行（去掉表头行与分隔行）。"""
    lines = [line for line in text.splitlines() if line.startswith("| ")]
    return [line for line in lines if "---" not in line][1:]


def quiz_head_count(markdown: str) -> int:
    """练习标题里声明的题数（没有练习板块就是 0）。"""
    match = re.search(r"小练习（(\d+) 题）", markdown)
    return int(match.group(1)) if match else 0


def vocab_words(markdown: str) -> list[str]:
    """词汇表里的单词列。"""
    rows = [row for row in table_rows(section(markdown, "核心词汇")) if row.count("|") >= 4]
    return [row.split("|")[2].strip().strip("*") for row in rows]


# ---------------------------------------------------------------- 角色配置
def test_load_roles_has_the_twelve_expected_keys(roles):
    assert list(roles) == list(EXPECTED_ROLES)
    assert len(EXPECTED_ROLES) == 12


def test_every_role_has_all_required_fields(roles):
    for role in roles.values():
        assert role.key and role.name
        assert role.levels and set(role.levels) <= set("A1 A2 B1 B2 C1 C2".split())
        assert role.modules and set(role.modules) <= set(PACK_MODULES)
        assert role.modules == tuple(name for name in SECTION_ORDER if name in role.modules)  # 已按文档顺序排好
        assert role.tone and isinstance(role.word_limit, int) and role.word_limit > 0
        assert isinstance(role.extra, dict) and role.extra


def test_roles_declare_focus_that_matches_their_audience(roles):
    """侧重要写在配置里，看得见也改得动。"""
    assert set(roles["kids"].levels) == {"A1", "A2"}                 # 少儿只用 A1/A2
    assert roles["kids"].extra["vocab_count"] >= 10                  # 并且多词汇
    assert set(roles["developer"].levels) == {"B2", "C1"}            # 技术侧重 B2-C1
    assert {"data", "server", "api"} <= set(roles["developer"].keywords)
    assert "商务" in roles["trade"].extra["focus"] or "关税" in roles["trade"].extra["focus"]
    assert "bilingual" in roles["trade"].modules                     # 外贸要能照着念给客户
    assert "writing" in roles["ielts_toefl"].modules                 # 雅思托福重写作
    assert "listening_script" in roles["cet"].modules                # 四六级重听力


def test_word_limits_are_ranked_by_how_heavy_the_role_is(roles):
    assert roles["kids"].word_limit < roles["postgrad"].word_limit
    assert roles["crossborder"].word_limit < roles["ielts_toefl"].word_limit
    assert roles["kids"].body_words < roles["academic"].body_words


def test_modules_accept_aliases_and_get_reordered(tmp_path):
    path = write_roles(tmp_path, """
roles:
  mini:
    name: 迷你角色
    levels: [b1]
    modules: [vocab, reading, exercises]
    tone: 简洁
    word_limit: 300
""")
    role = load_roles(path)["mini"]
    assert role.modules == ("levels", "vocabulary", "quiz")
    assert role.levels == ("B1",)


def test_roles_can_be_a_list_of_entries(tmp_path):
    path = write_roles(tmp_path, """
roles:
  - {key: dev, name: 开发, levels: [B2], modules: [levels, vocabulary], tone: 直给, word_limit: 300}
""")
    assert list(load_roles(path)) == ["dev"]


# ---------------------------------------------------------------- 配置报错
def test_missing_roles_file_raises(tmp_path):
    with pytest.raises(RolesError, match="找不到角色配置"):
        load_roles(tmp_path / "config" / ROLES_FILE_NAME)


@pytest.mark.parametrize("payload", ["", "roles:\n", "roles: []\n", "just: text\n"])
def test_useless_roles_files_raise_readable_errors(tmp_path, payload):
    with pytest.raises(RolesError, match="roles|空|映射"):
        load_roles(write_roles(tmp_path, payload))


def test_broken_yaml_reports_the_file(tmp_path):
    with pytest.raises(RolesError, match="不是合法 YAML"):
        load_roles(write_roles(tmp_path, "roles:\n  a: {name: 缺括号\n"))


@pytest.mark.parametrize(
    "body, message",
    [
        ("levels: [B9]\n", "非 CEFR 档位"),
        ("levels: []\n", "至少要有一个 levels"),
        ("modules: [nonsense]\n", "modules 有问题"),
        ("word_limit: 0\n", "正整数"),
        ("extra: 3\n", "必须是键值映射"),
    ],
)
def test_bad_role_fields_raise_readable_errors(tmp_path, body, message):
    path = write_roles(tmp_path, f"roles:\n  a:\n    {BASE_ROLE}{body}")
    with pytest.raises(RolesError, match=message):
        load_roles(path)


def test_entry_without_any_identifier_raises(tmp_path):
    path = write_roles(tmp_path, "roles:\n  - {levels: [B1], modules: [levels], tone: t, word_limit: 100}\n")
    with pytest.raises(RolesError, match="无法确定角色标识"):
        load_roles(path)


def test_duplicate_role_key_raises(tmp_path):
    path = write_roles(tmp_path, """
roles:
  kids: {name: 少儿, levels: [A1], modules: [levels], tone: t, word_limit: 100}
  KIDS: {name: 少儿2, levels: [A1], modules: [levels], tone: t, word_limit: 100}
""")
    with pytest.raises(RolesError, match="重复"):
        load_roles(path)


def test_roles_path_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    assert roles_path(tmp_path / "mine.yaml") == tmp_path / "mine.yaml"
    assert roles_path("config/roles.yaml") == tmp_path / "config" / "roles.yaml"
    assert roles_path().parent.parent == tmp_path  # 默认 <root>/config/roles.yaml


def test_role_for_is_lenient_on_keys_and_strict_about_unknowns(roles):
    assert role_for("IELTS-Toefl", roles).key == "ielts_toefl"
    assert role_for(" kids ", roles).key == "kids"
    with pytest.raises(RolesError, match="没有这个角色"):
        role_for("grandma", roles)


# ---------------------------------------------------------------- 验收：渲染 + 词数上限
@pytest.mark.parametrize("role_key", sorted(EXPECTED_ROLES))
def test_render_is_non_empty_and_respects_word_limit(pack, roles, role_key):
    """核心验收：每个角色都出稿、非空，整篇词数不超过它的 word_limit。"""
    markdown = render_for_role(pack, role_key, roles)
    assert markdown.strip(), role_key
    assert markdown.startswith("# "), role_key
    assert count_words(markdown) <= roles[role_key].word_limit, role_key
    assert markdown.count("\n## ") >= 3, role_key


@pytest.mark.parametrize("role_key", ["kids", "k12", "developer", "postgrad", "cet"])
def test_render_has_title_body_vocab_exercise_and_answer_fold(pack, roles, role_key):
    """标题 / 正文 / 词汇 / 练习 / 答案折叠区是产出骨架，一个都不能少。"""
    markdown = render_for_role(pack, role_key, roles)
    assert section(markdown, "正文"), role_key
    for marker in (HEADING_MARKERS["vocabulary"], HEADING_MARKERS["quiz"]):
        assert marker in markdown, (role_key, marker)
    assert "<details>" in markdown and "</details>" in markdown, role_key
    assert "<summary>" in markdown and "参考答案" in markdown, role_key


def test_answer_fold_holds_letters_and_explanations(pack, roles):
    fold = render_for_role(pack, "postgrad", roles).split("<summary>", 1)[1].split("</details>", 1)[0]
    assert "**B**" in fold or "B　" in fold or "答案速查" in fold
    assert "解析" in fold or "细节题" in fold or "答案速查" in fold


def test_word_limit_actually_bites(roles):
    """预算压到 150 词：仍然出稿、仍然不超限，并且明确告知被截断。"""
    squeezed = {key: replace(role, word_limit=150) for key, role in roles.items()}
    markdown = render_for_role(FIXTURE_PACK_DATA(), "postgrad", squeezed)
    assert markdown.startswith("# ")
    assert 0 < count_words(markdown) <= 150
    assert "截断" in markdown


def test_budget_is_spent_not_wasted(pack, roles):
    """压缩后应当贴着预算（回填生效），而不是砍到只剩三成。"""
    for key, role in roles.items():
        used = count_words(render_for_role(pack, key, roles))
        assert used >= 0.8 * role.word_limit, (key, used, role.word_limit)


@pytest.mark.parametrize("limit", [120, 250, 400, 800])
def test_squeezed_budgets_never_cross_the_limit_or_break_the_fold(pack, roles, limit):
    """预算压到任何档位都不越界；硬截断也不许把 <details> 撕成半个。"""
    squeezed = {key: replace(role, word_limit=limit) for key, role in roles.items()}
    for key in roles:
        markdown = render_for_role(pack, key, squeezed)
        assert 0 < count_words(markdown) <= limit, (key, limit, count_words(markdown))
        assert markdown.count("<details>") == markdown.count("</details>"), (key, limit)


def test_answer_fold_survives_a_tight_budget(pack, roles):
    """300 词的紧预算里，折叠区不该再被硬截断吃掉——至少一半角色还拿得到答案。"""
    squeezed = {key: replace(role, word_limit=300) for key, role in roles.items()}
    kept = [key for key in roles if "<details>" in render_for_role(pack, key, squeezed)]
    assert len(kept) >= 6, kept


def test_deep_trim_sacrifices_options_before_the_answers(tmp_path, pack):
    """最后一档：选项压没、题干留一行、答案折叠区还在（折叠区比选项值钱）。"""
    path = write_roles(tmp_path, """
roles:
  tight:
    name: 紧
    levels: [B1]
    modules: [levels, vocabulary, quiz]
    tone: 简洁
    word_limit: 200
    extra: {body_words: 30, vocab_count: 2, quiz_count: 5}
""")
    table = load_roles(path)
    assert MAX_DETAIL["quiz"] == 4  # 多出来的这一档 = 一行题干 + 答案速查
    markdown = render_for_role(pack, "tight", table)
    assert "- **A.**" not in markdown, "选项没先让路"
    assert re.search(r"^1\. Why did EU regulators fine the social network\?", markdown, re.M)
    assert "## 📝 小练习（2 题）" in markdown and "答案速查" in markdown


def test_body_respects_configured_body_words(tmp_path, pack):
    path = write_roles(tmp_path, """
roles:
  shorty:
    name: 短正文
    levels: [C1, B2]
    modules: [levels, vocabulary]
    tone: 精简
    word_limit: 400
    extra: {body_words: 40, vocab_count: 2}
""")
    role = load_roles(path)["shorty"]
    assert role.body_words == 40  # 配置说 40 就 40，不被地板抬高
    body = section(render_for_role(pack, "shorty", {role.key: role}), "正文").split("\n\n")[-1]
    assert 0 < count_words(body) <= 40
    assert body.endswith("…")  # 截短要说得清


def test_body_words_are_capped_at_half_the_total_budget(tmp_path, pack):
    path = write_roles(tmp_path, """
roles:
  greedy:
    name: 贪心
    levels: [C2]
    modules: [levels, vocabulary]
    tone: t
    word_limit: 200
    extra: {body_words: 5000, vocab_count: 6}
""")
    role = load_roles(path)["greedy"]
    assert role.body_words == 100  # 正文最多吃掉一半预算，剩下的留给词汇表
    markdown = render_for_role(pack, "greedy", {role.key: role})
    assert count_words(markdown) <= role.word_limit


def test_missing_master_keys_are_skipped_not_crashed(roles):
    """母版只生成了 levels + vocabulary（modules 只请求两个键）时，其余板块直接消失。"""
    partial = ContentPack(
        story_id="only-two",
        data={
            "levels": {"A1": {"title": "A cat", "body": "A cat sat down. " * 12}},
            "vocabulary": [{"word": "cat", "pos": "n.", "zh": "猫",
                            "en_def": "a small animal", "example": "The cat sat."}],
        },
    )
    markdown = render_for_role(partial, "kids", roles)
    assert "## 📖 正文 · CEFR A1" in markdown
    assert "## 📚 核心词汇" in markdown
    assert "## 📝 小练习" not in markdown
    assert "## 🧭 背景速览" not in markdown


def test_empty_pack_still_returns_a_document(roles):
    markdown = render_for_role(ContentPack(story_id="nothing-here", data={}), "k12", roles)
    assert markdown.startswith("# ")
    assert "nothing-here" in markdown
    assert count_words(markdown) <= roles["k12"].word_limit


def test_render_for_role_also_takes_a_plain_mapping(pack):
    """pipeline 里有时只有 dict（母版 JSON 的裸 data），也要能渲染。"""
    markdown = render_for_role(json.loads(FIXTURE.read_text(encoding="utf-8")), "cet")
    assert markdown.startswith("# ")
    assert "## 🎧 听力稿" in markdown


def test_render_is_deterministic(pack, roles):
    assert render_for_role(pack, "trade", roles) == render_for_role(pack, "trade", roles)


def FIXTURE_PACK_DATA():
    """150 词预算用的母版（fixture 的裸 data）。"""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 选材与角色侧重
def test_level_preference_is_honoured(pack, roles):
    assert "## 📖 正文 · CEFR A1" in render_for_role(pack, "kids", roles)
    assert "## 📖 正文 · CEFR A2" in render_for_role(pack, "crossborder", roles)
    assert "## 📖 正文 · CEFR B2" in render_for_role(pack, "developer", roles)
    assert "## 📖 正文 · CEFR B1" in render_for_role(pack, "cet", roles)
    assert "## 📖 正文 · CEFR C1" in render_for_role(pack, "academic", roles)


def test_kids_stays_in_a1_a2_and_floods_vocabulary(pack, roles):
    """少儿：难度只能是 A1/A2，词量还得是全场最多的一档（多词汇是它的立身之本）。"""
    markdown = render_for_role(pack, "kids", roles)
    assert [line for line in markdown.splitlines() if line.startswith("## 📖 正文")] == ["## 📖 正文 · CEFR A1"]
    hard = [line for line in markdown.splitlines() if line.startswith("## ") and ("CEFR B" in line or "CEFR C" in line)]
    assert not hard
    counts = {key: len(vocab_words(render_for_role(pack, key, roles))) for key in roles}
    assert counts["kids"] >= 10
    assert counts["kids"] == max(counts.values()), counts


def test_each_role_never_uses_modules_it_did_not_declare(pack, roles):
    """只吃自己声明的母版键：没声明的绝不出现，声明了的允许因预算被砍掉。"""
    for key, role in roles.items():
        markdown = render_for_role(pack, key, roles)
        for module, marker in HEADING_MARKERS.items():
            if marker in markdown:
                assert module in role.modules, (key, module)
        assert HEADING_MARKERS["levels"] in markdown  # 正文是核心，预算再紧也不许整段消失


def test_developer_focuses_on_tech_content(pack, roles):
    """程序员：B2/C1 + 逐句/长难句 + 技术题材词命中（fixture 里的 data / server）。"""
    markdown = render_for_role(pack, "developer", roles)
    role = roles["developer"]
    assert "## 🔎 逐句精读" in markdown and "## 🔧 长难句拆解" in markdown
    assert {"data", "server", "transfer"} <= set(role.keywords)
    assert "⭐" in markdown
    assert "data" in markdown.lower() and "server" in markdown.lower()
    assert topic_match(pack, "developer", roles) >= topic_match(pack, "kids", roles)
    assert "incident summary" in markdown  # 收尾任务也是技术活


def test_trade_and_business_roles_emphasise_ready_to_send_language(pack, roles):
    """外贸/商务：有可照念的对照句式 + 口语话术，题材词命中商务方向。"""
    for key in ("trade", "business"):
        markdown = render_for_role(pack, key, roles)
        assert "## 🗣 开口说" in markdown, key
        assert topic_match(pack, key, roles) > 0, key
    assert "询盘" in roles["trade"].tone
    assert "邮件" in roles["business"].extra["focus"]


def test_immigration_and_kids_pick_easy_bands_while_academic_picks_hard(pack, roles):
    assert set(roles["immigration"].levels) == {"A2", "B1"}
    assert set(roles["academic"].levels) == {"C1", "C2"}
    markdown = render_for_role(pack, "immigration", roles)
    assert "## 🔎 逐句精读" in markdown and "## 🗣 开口说" in markdown


def test_level_fallback_when_master_lacks_the_band(roles):
    """母版只有 A1 时，考研角色不能报错：就近回退并在开头标明。"""
    easy = ContentPack(story_id="easy-only",
                       data={"levels": {"A1": {"title": "Sun", "body": "The sun is hot. " * 14}}})
    markdown = render_for_role(easy, "postgrad", roles)
    assert "已就近回退" in markdown
    assert "## 📖 正文 · CEFR A1" in markdown


def test_trim_order_decides_what_survives(tmp_path, pack):
    """同样砍预算，声明了 trim_order 的角色会先牺牲自己不在乎的板块。"""
    path = write_roles(tmp_path, """
roles:
  keep_vocab:
    name: 保词汇
    levels: [B1]
    modules: [levels, vocabulary, quiz]
    tone: t
    word_limit: 300
    extra: {body_words: 50, vocab_count: 5, quiz_count: 3, trim_order: [quiz, vocabulary]}
  keep_quiz:
    name: 保练习
    levels: [B1]
    modules: [levels, vocabulary, quiz]
    tone: t
    word_limit: 300
    extra: {body_words: 50, vocab_count: 5, quiz_count: 3, trim_order: [vocabulary, quiz]}
""")
    table = load_roles(path)
    vocab_first = render_for_role(pack, "keep_vocab", table)
    quiz_first = render_for_role(pack, "keep_quiz", table)
    assert 0 < count_words(vocab_first) <= 300 and 0 < count_words(quiz_first) <= 300
    # 谁被声明为“先牺牲”，谁就先缩水
    assert len(vocab_words(vocab_first)) > len(vocab_words(quiz_first)), "词汇没被保住"
    assert quiz_head_count(quiz_first) > quiz_head_count(vocab_first), "练习没被保住"


def test_default_trim_order_is_sane():
    assert DEFAULT_TRIM_ORDER[-1] == "levels"
    assert DEFAULT_TRIM_ORDER.index("vocabulary") > DEFAULT_TRIM_ORDER.index("quiz")
    assert set(DEFAULT_TRIM_ORDER) == set(SECTION_ORDER) == set(MAX_DETAIL)


# ---------------------------------------------------------------- 契合度、批量与 CLI
def test_topic_match_is_a_sane_ratio(pack, roles):
    for key in roles:
        assert 0.0 <= topic_match(pack, key, roles) <= 1.0
    assert topic_match(pack, "developer", roles) > topic_match(pack, "k12", roles)
    assert topic_match(ContentPack(story_id="x", data={}), "postgrad", roles) == 0.0


def test_topic_match_zero_without_keywords(tmp_path, pack):
    path = write_roles(tmp_path, """
roles:
  bare:
    name: 裸
    levels: [B1]
    modules: [levels]
    tone: t
    word_limit: 200
""")
    table = load_roles(path)
    assert topic_match(pack, "bare", table) == 0.0
    assert table["bare"].tts_hint() == {}


def test_render_all_covers_every_role_within_budget(pack, roles):
    output = render_all(pack, roles=roles)
    assert list(output) == list(roles)
    for key, markdown in output.items():
        assert count_words(markdown) <= roles[key].word_limit
    assert list(render_all(pack, ["kids", "trade"], roles)) == ["kids", "trade"]


def test_uses_repo_config_by_default(pack):
    assert "## 🎧 听力稿" in render_for_role(pack, "cet")


def test_tts_hints_are_carried_through_the_config(roles):
    from foundry.config import load_settings

    voices = set(load_settings().tts.voices)
    for role in roles.values():
        hint = role.tts_hint()
        assert hint["tts_rate"].startswith(("+", "-"))
        assert hint["tts_voice"] in voices, hint["tts_voice"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", 0),
        ("hello world", 2),
        ("欧盟罚款 430 million euros", 7),
        ("Don't give up - 坚持!", 5),
        ("|---|---|", 0),
        ("# 📖 正文 · CEFR A1", 5),
    ],
)
def test_count_words_counts_english_words_cjk_chars_and_numbers(text, expected):
    assert count_words(text) == expected


def test_cli_list_prints_the_twelve_roles(capsys):
    assert roles_main(["--list"]) == 0
    printed = capsys.readouterr().out
    assert "共 12 个角色" in printed
    assert "kids" in printed and "developer" in printed


def test_cli_renders_and_writes_files(tmp_path, capsys):
    out_dir = tmp_path / "posts"
    assert roles_main(["--pack", str(FIXTURE), "--role", "kids,developer", "--out", str(out_dir)]) == 0
    assert (out_dir / "kids.md").read_text(encoding="utf-8").startswith("# 🧒")
    assert (out_dir / "developer.md").read_text(encoding="utf-8").startswith("# 💻")


def test_cli_reports_bad_requests(tmp_path, capsys):
    assert roles_main(["--pack", str(FIXTURE), "--role", "grandpa"]) == 1
    assert "没有这个角色" in capsys.readouterr().out
    assert roles_main(["--roles-file", str(tmp_path / "missing.yaml"), "--list"]) == 1
    assert "找不到角色配置" in capsys.readouterr().out

"""foundry.master 测试：全程离线，用假 LLM 客户端（返回 tests/fixtures/sample_pack.json）。

覆盖：modules 选择、JSON 容错解析、tenacity/内置退避重试、结果整形与校验、
落盘 output/packs/<story_id>.json、以及 openai SDK 缺失时的 requests 兜底通道。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry import master
from foundry.config import ENV_API_KEY, ENV_BASE_URL, ENV_MODEL, ENV_ROOT, ConfigError, load_settings
from foundry.master import (
    CEFR_LEVELS,
    FALLBACK_PROMPT,
    PACK_MODULES,
    MasterError,
    RetryableLLMError,
    build_client,
    clean_pack,
    generate_pack,
    load_prompt,
    parse_json_response,
    render_prompt,
    resolve_modules,
    story_view,
    validate_pack,
)
from foundry.models import Article, ContentPack, Story

FIXTURE = Path(__file__).parent / "fixtures" / "sample_pack.json"
SETTINGS_YAML = """
llm:
  base_url: https://llm.test/v1
  model: test-model
  temperature: 0.2
  timeout: 5
  max_retries: 2
"""


def pack_payload() -> dict:
    """fixture 里的母版（去掉 story_id，模拟 LLM 的真实返回）。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.pop("story_id")
    return data


class FakeClient:
    """假的 OpenAI 兼容客户端：记录 kwargs，按顺序吐出 replies（异常会被抛出）。"""

    def __init__(self, replies: list[object]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, dict):  # requests 兜底通道返回的就是 dict
            return reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    @property
    def sent(self) -> int:
        return len(self.calls)

    def user_prompt(self, index: int = 0) -> str:
        messages = self.calls[index]["messages"]
        assert [item["role"] for item in messages] == ["system", "user"]
        return messages[1]["content"]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """不继承本机 .env / 环境变量，也不真的退避等待。"""
    for name in (ENV_API_KEY, ENV_BASE_URL, ENV_MODEL, ENV_ROOT):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ENV_API_KEY, "test-key")
    monkeypatch.setattr(master.time, "sleep", lambda *_args: None)


@pytest.fixture
def settings(tmp_path):
    """隔离的项目根：pyproject.toml + config/settings.yaml，LLM 指向 https://llm.test。"""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "foundry"\n', encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(SETTINGS_YAML, encoding="utf-8")
    return load_settings(tmp_path)


@pytest.fixture
def story() -> Story:
    first = Article(
        source="demo_world",
        title="EU fines social network 430 million euros over data transfers",
        url="https://demo.example/a",
        content="Regulators in the European Union have fined a large social network 430 million euros. "
        "The company sent personal data of European users to servers in the United States.",
    )
    second = Article(
        source="npr_world",
        title="EU fines social network 430 million euros over data transfers",
        url="https://demo.example/b",
        content="Officials ordered the company to stop the transfers within six months.",
    )
    return Story(title="EU fines social network", url="https://demo.example/a", articles=[first, second])


@pytest.fixture
def good_reply() -> str:
    return json.dumps(pack_payload(), ensure_ascii=False)


# ---------------------------------------------------------------- 全流程
def test_generate_pack_returns_and_writes_full_pack(settings, story, good_reply):
    client = FakeClient([f"以下是结果：\n```json\n{good_reply}\n```"])
    pack = generate_pack(story, settings, client=client)

    assert list(pack.data) == list(PACK_MODULES)
    assert pack.story_id == story.id
    assert pack.path == settings.output.packs / f"{story.id}.json"
    assert pack.path.is_file()
    assert validate_pack(pack.data) == []
    assert pack.get("levels")["B1"]["title"].startswith("European regulators")

    reloaded = ContentPack.load(pack.path)
    assert reloaded.story_id == story.id
    assert len(reloaded.get("vocabulary")) == 12
    assert json.loads(pack.path.read_text(encoding="utf-8"))["story_id"] == story.id


def test_request_carries_openai_compatible_options(settings, story, good_reply):
    client = FakeClient([good_reply])
    generate_pack(story, settings, client=client)

    kwargs = client.calls[0]
    assert kwargs["model"] == "test-model"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["temperature"] == pytest.approx(0.2)
    assert kwargs["timeout"] == 5
    assert "JSON" in kwargs["messages"][0]["content"]
    prompt = client.user_prompt()
    assert "https://llm.test" not in prompt  # Key/endpoint 不进提示词
    assert "EU fines social network" in prompt
    assert "Regulators in the European Union" in prompt


def test_generate_pack_does_not_write_when_save_false(settings, story, good_reply):
    pack = generate_pack(story, settings, client=FakeClient([good_reply]), save=False)
    assert pack.path is None
    assert not (settings.output.packs / f"{story.id}.json").exists()


# ---------------------------------------------------------------- modules 选择
@pytest.mark.parametrize(
    "argument, expected",
    [
        (None, PACK_MODULES),
        ("all", PACK_MODULES),
        (["levels", "quiz"], ("levels", "quiz")),
        ("quiz,levels", ("levels", "quiz")),  # 顺序按规范顺序，不按传参顺序
        ("vocab,listening,background", ("vocabulary", "background_zh", "listening_script")),
    ],
)
def test_resolve_modules(argument, expected):
    assert resolve_modules(argument) == expected


def test_resolve_modules_rejects_unknown_name():
    with pytest.raises(MasterError, match="未知的模块名"):
        resolve_modules(["levels", "nope"])


def test_modules_limits_generated_keys_and_prompt(settings, story, good_reply):
    client = FakeClient([good_reply])
    pack = generate_pack(story, settings, modules="levels,quiz", client=client)

    assert list(pack.data) == ["levels", "quiz"]
    prompt = client.user_prompt()
    assert "## 本次需要的键\n\nlevels、quiz" in prompt  # 模板里只点名这两个键
    assert "EU fines social network" in prompt


def test_unknown_module_raises_before_any_network_call(settings, story):
    client = FakeClient([{}])
    with pytest.raises(MasterError, match="未知的模块名"):
        generate_pack(story, settings, modules=["typo"], client=client)
    assert client.sent == 0


def test_missing_keys_are_filled_with_empty_values(settings, story, caplog):
    reply = '{"levels": {"A1": {"title": "A small app", "body": "Europe says no."}}}'
    with caplog.at_level("WARNING"):
        pack = generate_pack(story, settings, modules=["levels", "vocabulary", "quiz"], client=FakeClient([reply]))

    assert list(pack.data) == ["levels", "vocabulary", "quiz"]
    assert pack.data["vocabulary"] == [] and pack.data["quiz"] == []
    assert not pack.has("quiz")
    assert "没有产出键 quiz" in caplog.text
    assert any("期望 12 条" in problem for problem in validate_pack(pack.data))


# ---------------------------------------------------------------- JSON 容错
@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"a": 1}', {"a": 1}),
        ("```json\n{\"a\": 1}\n```", {"a": 1}),
        ("```JSON\n{\"a\": 1}\n```", {"a": 1}),
        ("```\n{\"a\": 1}\n```", {"a": 1}),
        ("好的，这是结果：\n```json\n{\"a\": {\"b\": [1, 2]}}\n```\n希望有帮助", {"a": {"b": [1, 2]}}),
        ("{\"a\": 1, \"b\": 2,}", {"a": 1, "b": 2}),  # 尾逗号
        ('{"a": "line1\nline2"}', {"a": "line1\nline2"}),  # 串内裸换行
        ('{"ok": true, "n": null}', {"ok": True, "n": None}),
    ],
)
def test_parse_json_response_is_forgiving(raw, expected):
    assert parse_json_response(raw) == expected


def test_parse_json_response_keeps_only_the_first_object():
    text = 'prefix {"a": {"nested": "}"}, "b": 2} suffix {"c": 3}'
    assert parse_json_response(text) == {"a": {"nested": "}"}, "b": 2}


@pytest.mark.parametrize("raw", ["", "   ", "抱歉，我无法完成", "[1, 2, 3]", '{"unclosed": 1,```'])
def test_parse_json_response_raises_retryable_on_junk(raw):
    with pytest.raises(RetryableLLMError):
        parse_json_response(raw)


def test_unparseable_reply_is_retried_then_succeeds(settings, story, good_reply):
    client = FakeClient(["抱歉，我无法完成", f"```json\n{good_reply}\n```"])
    pack = generate_pack(story, settings, modules=["quiz"], client=client, backoff=0)
    assert client.sent == 2
    assert len(pack.data["quiz"]) == 5


def test_retries_are_bounded_by_attempts(settings, story):
    client = FakeClient([RetryableLLMError("无法解析")])
    with pytest.raises(MasterError, match="无法解析"):
        generate_pack(story, settings, client=client, attempts=2, backoff=0)
    assert client.sent == 2


def test_transport_errors_are_retried(settings, story, good_reply):
    client = FakeClient([ConnectionError("socket closed"), good_reply])
    generate_pack(story, settings, client=client, backoff=0)
    assert client.sent == 2


def test_client_side_status_error_is_not_retried(settings, story):
    error = RuntimeError("bad request")
    error.status_code = 400  # 4xx 不空耗（与 fetcher 对 4xx 的口径一致）
    client = FakeClient([error])
    with pytest.raises(MasterError, match="bad request"):
        generate_pack(story, settings, client=client, attempts=3, backoff=0)
    assert client.sent == 1


# ---------------------------------------------------------------- 响应抽取与客户端
def test_extract_text_handles_dict_and_chunked_content():
    dict_response = {"choices": [{"message": {"content": "hello"}}]}
    assert master.extract_text(dict_response) == "hello"
    chunks = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": "a"}, {"text": "b"}]))])
    assert master.extract_text(chunks) == "ab"
    parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, parsed={"levels": {}}))])
    assert json.loads(master.extract_text(parsed)) == {"levels": {}}
    with pytest.raises(RetryableLLMError):
        master.extract_text({"detail": "no choices here"})


def test_build_client_uses_settings_and_falls_back_to_requests(settings, monkeypatch):
    monkeypatch.setattr(master, "OpenAI", None)  # 模拟没装 SDK 的机器
    client = build_client(settings)
    assert isinstance(client, master.RestChatClient)
    assert client.endpoint == "https://llm.test/v1/chat/completions"
    assert client.api_key == "test-key"
    assert client.timeout == 5.0


def test_requests_fallback_client_calls_chat_completions(settings, story, good_reply, monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse({"choices": [{"message": {"content": good_reply}}]})

    monkeypatch.setattr(master, "OpenAI", None)
    monkeypatch.setattr(master.requests, "post", fake_post)

    pack = generate_pack(story, settings, modules=["levels"], backoff=0)

    assert list(pack.data) == ["levels"]
    assert calls[0]["url"].endswith("/chat/completions")
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json"]["model"] == "test-model"
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert calls[0]["json"]["messages"][1]["role"] == "user"


def test_missing_api_key_raises_actionable_error(settings, story, monkeypatch):
    monkeypatch.delenv(ENV_API_KEY)
    keyless = load_settings(settings.root)
    client = FakeClient([{}])
    with pytest.raises(ConfigError, match=ENV_API_KEY):
        generate_pack(story, keyless)
    with pytest.raises(ConfigError, match=ENV_API_KEY):
        build_client(keyless)
    assert client.sent == 0  # 报错发生在出网之前


# ---------------------------------------------------------------- 输入整形
def test_story_view_from_story_joins_sources_and_keeps_id(story):
    view = story_view(story)
    assert view.id == story.id
    assert view.article_count == 2
    assert "[demo_world] " in view.body and "[npr_world] " in view.body
    assert view.source == "demo_world, npr_world"
    assert view.published == ""


def test_story_view_from_article_and_mapping():
    article = Article(title="Solo piece", url="https://demo.example/solo", content="One source body text.")
    assert story_view(article).id == article.id
    from_mapping = story_view({"title": "From dict", "content": "Dict body text.", "source": "dict"})
    assert from_mapping.source == "dict" and from_mapping.body.startswith("Dict body")


def test_story_view_truncates_very_long_bodies():
    article = Article(title="Long", url="https://demo.example/long", content="word " * 4000)
    view = story_view(article)
    assert len(view.body) <= master.MAX_BODY_CHARS
    assert view.body.endswith("…（正文已截断）")


def test_story_view_rejects_empty_input():
    with pytest.raises(MasterError, match="标题"):
        story_view({"title": "", "content": ""})


# ---------------------------------------------------------------- 整形与校验
def test_clean_pack_repairs_levels_and_quiz_answers():
    data = {
        "levels": {"a1": {"title": " T ", "body": " B ", "extra": 1}, "B1": {"title": "T"}, "XX": {"title": "x"}},
        "quiz": [
            {"question": "Q1", "choices": ["Alpha", "Beta"], "answer": "b. beta", "explain": "因为 B"},
            {"q": "Q2", "choices": ["Alpha", "Beta"], "answer": "Beta", "explain": "选项原文"},
            {"q": "Q3", "choices": ["Alpha", "Beta"], "answer": 1, "explain": "序号"},
        ],
        "story_id": "should-be-dropped",
    }
    cleaned = clean_pack(data, ["levels", "quiz"])

    assert list(cleaned) == ["levels", "quiz"]
    assert list(cleaned["levels"]) == ["A1", "B1"]
    assert cleaned["levels"]["A1"] == {"title": "T", "body": "B"}
    assert [item["answer"] for item in cleaned["quiz"]] == ["B", "B", "A"]
    assert cleaned["quiz"][0]["q"] == "Q1"


def test_validate_pack_reports_quality_problems():
    payload = pack_payload()
    payload["vocabulary"] = payload["vocabulary"][:11]
    payload["background_zh"] = "很长" * 100
    payload["writing"]["essay"] = "Too short to count as an essay."
    payload["quiz"][0]["answer"] = "E"
    payload["quiz"].pop()
    payload["speaking"]["roleplay"] = {}
    del payload["grammar"]

    problems = validate_pack(payload)
    assert any("vocabulary: 期望 12 条，实际 11 条" in item for item in problems)
    assert any("grammar: 缺失" == item for item in problems)
    assert any("background_zh 200 字" in item for item in problems)
    assert any("essay 7 词" in item for item in problems)
    assert any("quiz: 期望 5 条，实际 4 条" in item for item in problems)
    assert any("不在 A-D 范围内" in item for item in problems)
    assert any("roleplay 缺字段" in item for item in problems)


def test_validate_pack_accepts_the_fixture():
    assert validate_pack(pack_payload()) == []


# ---------------------------------------------------------------- 提示词
def test_prompt_file_documents_every_module():
    text = Path(master._default_prompts_dir() / "master.md").read_text(encoding="utf-8")
    for key in PACK_MODULES:
        assert f"`{key}`" in text or f"### `{key}`" in text, key
    for field in ("word", "pos", "zh", "en_def", "example", "sentence", "point", "explanation_zh", "choices", "answer"):
        assert field in text, field
    assert "只输出一个 JSON 对象" in text
    assert "{{MODULES}}" in text and "{{BODY}}" in text


def test_render_prompt_substitutes_placeholders(story):
    rendered = render_prompt(load_prompt(), story_view(story), resolve_modules(["levels"]))
    assert "{{" not in rendered
    assert "EU fines social network" in rendered
    assert "levels" in rendered


def test_load_prompt_falls_back_when_file_missing(settings, caplog):
    with caplog.at_level("WARNING"):
        text = load_prompt(name="definitely-missing.md", settings=settings)
    assert "{{MODULES}}" in text
    assert text is FALLBACK_PROMPT
    assert "使用内置精简版" in caplog.text


# ---------------------------------------------------------------- 命令行
def write_stories(settings, story) -> Path:
    settings.output.raw.mkdir(parents=True, exist_ok=True)
    target = settings.output.raw / "stories.json"
    target.write_text(json.dumps([story.to_dict()], ensure_ascii=False), encoding="utf-8")
    return target


def test_cli_dry_run_touches_no_network(settings, story, monkeypatch, capsys):
    monkeypatch.setenv(ENV_ROOT, str(settings.root))  # 让 _main 内部的 load_settings 也落在 tmp 项目
    source = write_stories(settings, story)

    def explode(*_args, **_kwargs):  # 任何出网尝试都会让用例失败
        raise AssertionError("dry-run 不应该调用 LLM")

    monkeypatch.setattr(master, "build_client", explode)
    code = master._main(["--input", str(source), "--limit", "1", "--modules", "levels,quiz", "--dry-run"])
    printed = capsys.readouterr().out

    assert code == 0
    assert "dry-run 未调用 LLM" in printed
    assert f"{story.id}.json" in printed
    assert not (settings.output.packs / f"{story.id}.json").exists()


def test_cli_writes_pack_and_reports_problems(settings, story, good_reply, monkeypatch, capsys):
    monkeypatch.setenv(ENV_ROOT, str(settings.root))  # 让 _main 内部的 load_settings 也落在 tmp 项目
    source = write_stories(settings, story)
    client = FakeClient(['{"levels": {"A1": {"title": "T", "body": "B"}}}'])
    monkeypatch.setattr(master, "build_client", lambda *_args, **_kwargs: client)

    code = master._main(["--input", str(source), "--modules", "levels,quiz"])
    printed = capsys.readouterr().out
    target = settings.output.packs / f"{story.id}.json"

    assert code == 0
    assert client.sent == 1
    assert target.is_file()
    assert ContentPack.load(target).data["quiz"] == []
    assert "项待改进" in printed


def test_cli_reports_missing_api_key_without_traceback(settings, story, monkeypatch, capsys):
    monkeypatch.setenv(ENV_ROOT, str(settings.root))  # 让 _main 内部的 load_settings 也落在 tmp 项目
    monkeypatch.delenv(ENV_API_KEY)
    source = write_stories(settings, story)

    assert master._main(["--input", str(source)]) == 1
    printed = capsys.readouterr().out
    assert ENV_API_KEY in printed and "Traceback" not in printed


def test_cli_reports_missing_input(settings, tmp_path, capsys):
    assert master._main(["--input", str(tmp_path / "nothing.json")]) == 1
    assert "foundry.normalizer" in capsys.readouterr().out


def test_cli_survives_a_failing_story(settings, story, monkeypatch, capsys):
    monkeypatch.setenv(ENV_ROOT, str(settings.root))  # 让 _main 内部的 load_settings 也落在 tmp 项目
    source = write_stories(settings, story)
    monkeypatch.setattr(master, "build_client", lambda *_args, **_kwargs: FakeClient([RetryableLLMError("额度用尽")]))
    assert master._main(["--input", str(source)]) == 1
    assert "失败" in capsys.readouterr().out


def test_cefr_levels_are_six_and_fixture_covers_them():
    assert len(CEFR_LEVELS) == 6
    assert set(pack_payload()["levels"]) == set(CEFR_LEVELS)

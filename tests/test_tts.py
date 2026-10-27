"""foundry.tts 测试：全程离线——用 monkeypatch 换掉 ``edge_tts.Communicate``，不碰微软的服务。

覆盖：synth_one 的入参与写盘（原子、非空、建父目录）、口音别名与语速归一、非法口音/空文本
不出网、拿不到音频/超时/重试后成功、batch_synth 的多形状输入与**单条失败隔离**、
overwrite=False 增量跳过、文件名不逃逸输出目录、事件循环里误用同步包装、manifest、
speech_text 的 Markdown 清洗、未装 edge-tts 的报错、roles.yaml 配音提示可解析，
以及命令行入口。联网验收（默认跳过）：``FOUNDRY_TTS_LIVE=1 pytest tests/test_tts.py -k live``。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from contextlib import redirect_stdout
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, NamedTuple, Sequence

import pytest

from foundry import tts
from foundry.config import ENV_ROOT, load_settings
from foundry.roles import load_roles
from foundry.tts import (
    AUDIO_SUFFIX,
    DEFAULT_RATE,
    DEFAULT_VOICE,
    VOICES,
    SynthItem,
    TtsError,
    TtsUnavailable,
    batch_synth,
    normalize_rate,
    resolve_voice,
    safe_name,
    speech_text,
    synth_one,
    target_path,
    write_manifest,
)
from foundry.tts import _main as tts_main

#: 一条“够用就行”的假 mp3（真文件头 + 噪声帧），用来验证字节完整落盘
MP3_PROBE = b"ID3\x03\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 300
MARKDOWN = """## 📝 EU fines hit 430 million euros

**Brussels** told [the vendor](https://example.com/eu) to pay up.

| word | meaning |
|---|---|
| fine | 罚款 |

- bullet one
- `inline code`

<details><summary>答案</summary>1. A</details>

---
*配音建议：us_female · 更慢一点*
"""
LIVE = os.environ.get("FOUNDRY_TTS_LIVE") == "1"
RATES: list[tuple[Any, str]] = [
    (None, "+0%"),
    ("+0%", "+0%"),
    ("0", "+0%"),
    (0, "+0%"),
    ("+5%", "+5%"),
    ("+5", "+5%"),
    ("-10%", "-10%"),
    (" -10 ", "-10%"),
    (10, "+10%"),
    ("-100%", "-100%"),
]


class DataType(str, Enum):
    """照抄 edge-tts 的块类型枚举（str 子类），验证本模块能认出 ``DataType.AUDIO``。"""

    AUDIO = "audio"
    WORD_BOUNDARY = "WordBoundary"


class FakeCall(NamedTuple):
    """一次被记录的 Communicate 调用。"""

    text: str
    voice: str | None
    rate: str | None
    kwargs: dict[str, Any]


def install_edge_tts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    audio: bytes = MP3_PROBE,
    failures: Sequence[BaseException | None] = (),
    fail_on: Callable[[str], BaseException | None] | None = None,
    sleep: float = 0.0,
) -> dict[str, Any]:
    """把 :mod:`foundry.tts` 里的 ``edge_tts`` 换成假的 ``Communicate``（本模块唯一出网口）。

    Args:
        audio: stream() 要吐出的音频字节；空字节表示“服务没返回音频”。
        failures: 按调用顺序给出要抛的异常，None 表示正常；用完按最后一项继续（测重试用）。
        fail_on: 按文本决定这一条抛不抛（批量用例里比顺序更稳）。
        sleep: stream() 前先睡这么久，用来测超时。

    Returns:
        状态字典，``state["calls"]`` 是 :class:`FakeCall` 列表（断言“到底把什么发给了 TTS”）。
    """
    state: dict[str, Any] = {"calls": [], "audio": audio, "failures": list(failures), "sleep": sleep}

    class Communicate:
        def __init__(self, text: str, voice: str | None = None, rate: str | None = None, **kwargs: Any) -> None:
            state["calls"].append(FakeCall(text, voice, rate, kwargs))
            self.index = len(state["calls"]) - 1

        async def stream(self):
            if state["sleep"]:
                await asyncio.sleep(state["sleep"])
            if fail_on is not None:
                error = fail_on(state["calls"][self.index].text)
                if error is not None:
                    raise error
            errors = state["failures"]
            if errors:
                error = errors[self.index] if self.index < len(errors) else errors[-1]
                if error is not None:
                    raise error
            payload = state["audio"]
            if not payload:
                return
            half = len(payload) // 2
            yield {"type": "audio", "data": payload[:half]}
            yield {"type": "WordBoundary", "offset": 0, "duration": 2_500_000, "text": "Hello"}
            yield {"type": "audio", "data": payload[half:]}

    monkeypatch.setattr(tts, "edge_tts", SimpleNamespace(Communicate=Communicate))
    return state


def call_for(state: dict[str, Any], needle: str) -> FakeCall:
    """按文本片段取那次调用（并发下顺序不保证，所以按内容找）。"""
    matches = [call for call in state["calls"] if needle in call.text]
    assert len(matches) == 1, (needle, [call.text for call in state["calls"]])
    return matches[0]


def run(coroutine: Any) -> Any:
    """小助手：同步跑一个协程。"""
    return asyncio.run(coroutine)


# ---------------------------------------------------------------- synth_one
def test_synth_one_writes_nonempty_mp3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    out = run(synth_one("Hello from EnglishFoundry, the global English factory.", tmp_path / "hello.mp3"))

    assert out == (tmp_path / "hello.mp3").resolve()
    assert out.suffix == AUDIO_SUFFIX
    assert out.read_bytes() == MP3_PROBE  # 两个音频块拼回完整字节
    assert out.stat().st_size > 1000
    assert list(tmp_path.iterdir()) == [out]  # 不留 .part 残骸
    call = state["calls"][0]
    assert call.text == "Hello from EnglishFoundry, the global English factory."
    assert (call.voice, call.rate) == (DEFAULT_VOICE, DEFAULT_RATE) == ("en-US-AriaNeural", "+0%")


@pytest.mark.parametrize("alias", sorted(VOICES))
def test_synth_one_resolves_voice_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str) -> None:
    state = install_edge_tts(monkeypatch)
    run(synth_one(f"Any English sentence will do here, {alias}.", tmp_path / "a.mp3", alias))
    assert state["calls"][0].voice == VOICES[alias]


@pytest.mark.parametrize(
    ("voice", "expected"),
    [
        ("en-US-AriaNeural", "en-US-AriaNeural"),
        ("en-US-GuyNeural", "en-US-GuyNeural"),
        ("en-GB-SoniaNeural", "en-GB-SoniaNeural"),
        ("en-AU-NatashaNeural", "en-AU-NatashaNeural"),
        ("zh-CN-XiaoxiaoNeural", "zh-CN-XiaoxiaoNeural"),  # 清单外的合法名字照传
    ],
)
def test_voice_names_pass_through(voice: str, expected: str) -> None:
    assert resolve_voice(voice) == expected


def test_voice_aliases_match_shipped_config() -> None:
    """内置别名要和 config/settings.yaml 的 ``tts.voices`` 一致，否则角色提示会落空。"""
    assert dict(load_settings().tts.voices) == dict(VOICES)


def test_role_tts_hints_are_resolvable() -> None:
    """roles.yaml 里透传的 tts_voice / tts_rate 必须能被本模块吃下。"""
    hints = [role.tts_hint() for role in load_roles().values()]
    assert hints, "roles.yaml 应该带着配音提示"
    for hint in hints:
        assert resolve_voice(hint["tts_voice"]) in set(VOICES.values())
        assert normalize_rate(hint["tts_rate"]) == hint["tts_rate"]


@pytest.mark.parametrize(("value", "expected"), RATES)
def test_normalize_rate(value: Any, expected: str) -> None:
    assert normalize_rate(value) == expected


@pytest.mark.parametrize("bad", ["fast", "+-1%", "10 percent", "abc%", "200%", "+999"])
def test_normalize_rate_rejects_garbage(bad: str) -> None:
    with pytest.raises(TtsError, match="语速"):
        normalize_rate(bad)


@pytest.mark.parametrize("bad", ["not a voice", "Aria", "en-US-Aria Neural", "en@@US@@Aria", "口音名"])
def test_bad_voice_fails_before_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    state = install_edge_tts(monkeypatch)
    with pytest.raises(TtsError, match="口音"):
        run(synth_one("Hello world", tmp_path / "a.mp3", bad))
    assert state["calls"] == []  # 没出网
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("empty", ["", "   ", "\n\n  \t", "** **", "📝 🎯 ⭐", "[](https://x.y)"])
def test_empty_text_fails_before_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: str) -> None:
    state = install_edge_tts(monkeypatch)
    with pytest.raises(TtsError, match="没有可朗读的文本"):
        run(synth_one(empty, tmp_path / "a.mp3"))
    assert state["calls"] == []
    assert list(tmp_path.iterdir()) == []


def test_empty_voice_and_rate_fall_back_to_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """voice/rate 传空串表示“用配置默认”，和 None 一样。"""
    state = install_edge_tts(monkeypatch)
    run(synth_one("Default voice please.", tmp_path / "a.mp3", "", ""))
    assert (state["calls"][0].voice, state["calls"][0].rate) == (DEFAULT_VOICE, DEFAULT_RATE)


def test_synth_one_creates_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch)
    out = run(synth_one("Nested output is fine.", tmp_path / "audio" / "deep" / "x.mp3"))
    assert out.is_file()


def test_no_audio_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch, audio=b"")
    with pytest.raises(TtsError, match="没有返回音频"):
        run(synth_one("Hello world", tmp_path / "a.mp3", attempts=1))
    assert list(tmp_path.iterdir()) == []  # 失败不落地半个文件


def test_transient_error_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch, failures=[ConnectionError("drain"), None])
    out = run(synth_one("Hello world", tmp_path / "a.mp3", attempts=3, backoff=0))
    assert out.read_bytes() == MP3_PROBE
    assert len(state["calls"]) == 2


def test_retries_exhausted_reports_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch, failures=[ConnectionError("socket closed")])
    with pytest.raises(TtsError, match="socket closed") as caught:
        run(synth_one("Hello world", tmp_path / "a.mp3", attempts=2, backoff=0))
    assert "试了 2 次" in str(caught.value)
    assert len(state["calls"]) == 2
    assert list(tmp_path.iterdir()) == []


def test_timeout_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch, sleep=1.0)
    with pytest.raises(TtsError, match="内没合成完"):
        run(synth_one("Hello world", tmp_path / "a.mp3", attempts=1, timeout=0.01))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "chunk",
    [
        {"type": "audio", "data": b"abc"},
        {"type": "AUDIO", "data": bytearray(b"abc")},
        {"type": DataType.AUDIO, "data": memoryview(b"abc")},  # 真库给的是 str 枚举
        SimpleNamespace(type=DataType.AUDIO, data=b"abc"),
    ],
)
def test_chunk_audio_accepts_real_edge_tts_shapes(chunk: Any) -> None:
    assert tts._chunk_audio(chunk) == b"abc"


@pytest.mark.parametrize("chunk", [{"type": DataType.WORD_BOUNDARY, "data": b"abc"}, {"type": "audio"}, SimpleNamespace(type="sentence", data=b"x")])
def test_non_audio_chunks_are_skipped(chunk: Any) -> None:
    assert tts._chunk_audio(chunk) == b""


def test_word_boundary_chunks_do_not_leak_into_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    run(synth_one("Hello world", tmp_path / "a.mp3"))
    out = (tmp_path / "a.mp3").read_bytes()
    assert out == MP3_PROBE and b"Hello" not in out  # 假 stream 里夹的 WordBoundary 没被当音频
    assert state["calls"][0].kwargs == {}  # 只传 voice / rate，不塞私有参数


# ---------------------------------------------------------------- 缺依赖
def test_missing_edge_tts_gives_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts, "edge_tts", None)
    with pytest.raises(TtsUnavailable, match="pip install"):
        run(synth_one("Hello world", tmp_path / "a.mp3"))


def test_edge_tts_without_communicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts, "edge_tts", SimpleNamespace(__version__="0.0.1"))
    with pytest.raises(TtsUnavailable, match="Communicate"):
        run(synth_one("Hello world", tmp_path / "a.mp3"))


# ---------------------------------------------------------------- 文本清洗
def test_speech_text_strips_markdown_scaffolding() -> None:
    spoken = speech_text(MARKDOWN)

    for marker in ("#", "**", "*", "|", "```", "https://", "example.com", "📝", "<", ">", "·", "---", "](  "):
        assert marker not in spoken, marker
    for kept in ("EU fines hit 430 million euros", "Brussels", "the vendor", "bullet one", "inline code", "答案", "1. A"):
        assert kept in spoken, kept
    assert "  " not in spoken  # 空白被压干净了


def test_speech_text_leaves_plain_text_alone() -> None:
    text = "The European Commission fined the company 430 million euros on Tuesday."
    assert speech_text(text) == text


def test_synth_one_cleans_markup_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    run(synth_one(MARKDOWN, tmp_path / "a.mp3"))
    assert state["calls"][0].text == speech_text(MARKDOWN)


def test_synth_one_can_send_text_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    run(synth_one(MARKDOWN, tmp_path / "a.mp3", strip_markup=False))
    assert state["calls"][0].text == MARKDOWN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello-world", "hello-world"),
        ("Hello World! 罚款 430m", "Hello-World-罚款-430m"),
        ("../../etc/passwd", "passwd"),
        ("a" * 200, "a" * 80),
        ("clip.mp3", "clip"),
        ("  spaced  name  ", "spaced-name"),
        ("weird/../..", ""),
    ],
)
def test_safe_name(raw: str, expected: str) -> None:
    assert safe_name(raw) == expected


def test_target_path_always_lives_in_out_dir(tmp_path: Path) -> None:
    path = target_path(tmp_path, "../../etc/passwd.mp3")
    assert path.parent == tmp_path
    assert path.name == "passwd.mp3"
    assert target_path(tmp_path, "weird/../..").name == "clip.mp3"  # 退化 key 也有兜底文件名


# ---------------------------------------------------------------- batch_synth
def test_batch_synth_isolates_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch, fail_on=lambda text: RuntimeError("boom") if "boom" in text else None)
    results = batch_synth(
        {
            "alpha": "Alpha bravo charlie delta echo foxtrot.",
            "bad-guy": "This one will boom in the middle of the batch.",
            "blank": "   ",
            "zulu": "Zulu yankee xray whiskey victor uniform tango.",
        },
        tmp_path,
        attempts=1,
    )

    assert [row.key for row in results] == ["alpha", "bad-guy", "blank", "zulu"]  # 顺序不变
    assert [row.ok for row in results] == [True, False, False, True]
    assert "RuntimeError: boom" in results[1].error
    assert "没有可朗读的文本" in results[2].error
    assert (tmp_path / "alpha.mp3").read_bytes() == MP3_PROBE
    assert (tmp_path / "zulu.mp3").is_file()
    assert not (tmp_path / "bad-guy.mp3").exists()
    assert not (tmp_path / "blank.mp3").exists()
    assert len(state["calls"]) == 3  # 空文本那条压根没出网


@pytest.mark.parametrize("workers", [1, 3])
def test_batch_synth_workers_do_not_change_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int) -> None:
    install_edge_tts(monkeypatch)
    items = {f"clip-{index}": f"Sentence number {index} for the listening deck." for index in range(6)}
    results = batch_synth(items, tmp_path, workers=workers, attempts=1)

    assert all(row.ok for row in results)
    assert [row.key for row in results] == list(items)
    assert sorted(path.name for path in tmp_path.glob("*.mp3")) == sorted(f"{key}{AUDIO_SUFFIX}" for key in items)


def test_batch_synth_accepts_many_input_shapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    results = batch_synth(
        [
            SynthItem(key="item", text="SynthItem sentence."),
            ("tuple-key", "Two tuple sentence."),
            ("three", "Three tuple sentence.", "en-US-GuyNeural"),
            ("four", "Four tuple sentence.", "uk_female", "-10%"),
            {"key": "map", "text": "Mapping sentence.", "tts_voice": "us_male", "tts_rate": "+5%"},
            {"name": "named", "content": "Named content sentence."},
            SimpleNamespace(id="object", body="Object field sentence.", tts_voice="au_female"),
            "Bare string sentence.",
        ],
        tmp_path,
        attempts=1,
    )

    assert all(row.ok for row in results)
    assert sorted(path.name for path in tmp_path.glob("*.mp3")) == [
        "clip-08.mp3",
        "four.mp3",
        "item.mp3",
        "map.mp3",
        "named.mp3",
        "object.mp3",
        "three.mp3",
        "tuple-key.mp3",
    ]
    assert call_for(state, "SynthItem sentence").voice == DEFAULT_VOICE
    assert call_for(state, "Two tuple sentence").voice == DEFAULT_VOICE
    assert call_for(state, "Three tuple sentence").voice == "en-US-GuyNeural"
    assert call_for(state, "Four tuple sentence").voice == "en-GB-SoniaNeural"
    assert call_for(state, "Four tuple sentence").rate == "-10%"
    assert call_for(state, "Mapping sentence").voice == "en-US-GuyNeural"
    assert call_for(state, "Mapping sentence").rate == "+5%"
    assert call_for(state, "Named content sentence").voice == DEFAULT_VOICE
    assert call_for(state, "Object field sentence").voice == "en-AU-NatashaNeural"
    assert call_for(state, "Bare string sentence").voice == DEFAULT_VOICE


def test_batch_synth_batch_voice_can_be_overridden_per_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    results = batch_synth(
        {"one": "First batch sentence.", "two": "Second batch sentence."},
        tmp_path,
        voice="uk_female",
        rate="+8%",
        attempts=1,
    )

    assert [row.voice for row in results] == ["en-GB-SoniaNeural", "en-GB-SoniaNeural"]
    assert [row.rate for row in results] == ["+8%", "+8%"]
    assert {call.voice for call in state["calls"]} == {"en-GB-SoniaNeural"}


def test_batch_synth_skip_existing_is_incremental(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = b"already-synthesized-audio-bytes-" * 20
    (tmp_path / "keep.mp3").write_bytes(existing)
    state = install_edge_tts(monkeypatch)
    results = batch_synth({"keep": "Brand new text.", "fresh": "Also brand new text."}, tmp_path, overwrite=False, attempts=1)

    assert [row.skipped for row in results] == [True, False]
    assert results[0].ok and results[0].bytes == len(existing)
    assert (tmp_path / "keep.mp3").read_bytes() == existing
    assert [call.text for call in state["calls"]] == ["Also brand new text."]


def test_batch_synth_sanitizes_keys_into_out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch)
    out = tmp_path / "audio"
    results = batch_synth({"../../etc/passwd": "Escape attempt.", "📝 小练习": "Emoji key."}, out, attempts=1)

    assert [row.path for row in results] == [out / "passwd.mp3", out / "小练习.mp3"]
    assert sorted(path.name for path in out.iterdir()) == ["passwd.mp3", "小练习.mp3"]
    assert not (tmp_path / "passwd.mp3").exists()


def test_batch_synth_handles_empty_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch)
    assert batch_synth([], tmp_path) == []
    assert batch_synth({}, tmp_path) == []
    assert batch_synth(None, tmp_path) == []


def test_batch_synth_refuses_to_run_inside_a_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch)

    async def drive() -> str:
        try:
            batch_synth({"x": "Hello world."}, tmp_path)
        except TtsError as error:
            return str(error)
        return ""

    assert "事件循环" in run(drive())


# ---------------------------------------------------------------- manifest
def test_write_manifest_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch, fail_on=lambda text: RuntimeError("nope") if "nope" in text else None)
    results = batch_synth({"good": "Good sentence.", "bad": "This is a nope case."}, tmp_path, attempts=1)
    path = write_manifest(results, tmp_path / "manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["total"] == 2 and payload["succeeded"] == 1 and payload["failed"] == 1
    assert payload["voice"] == DEFAULT_VOICE and payload["rate"] == DEFAULT_RATE
    rows = {row["key"]: row for row in payload["items"]}
    assert rows["good"]["file"] == "good.mp3" and rows["good"]["bytes"] == len(MP3_PROBE)
    assert rows["bad"]["file"] is None and "RuntimeError: nope" in rows["bad"]["error"]


def test_write_manifest_defaults_under_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))  # 不污染仓库的 output/
    settings = load_settings(tmp_path)
    path = write_manifest([], settings=settings)
    assert path.parent == settings.output.audio
    assert path.name == "manifest.json" and path.is_file()


def test_role_markdown_becomes_speakable_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """角色渲染出来的 Markdown 直接喂给配音：清洗后没有 Markdown 残骸，正文还在。"""
    from foundry.models import ContentPack
    from foundry.roles import render_for_role

    pack = ContentPack.load(Path(__file__).parent / "fixtures" / "sample_pack.json")
    markdown = render_for_role(pack, "business")
    state = install_edge_tts(monkeypatch)
    out = run(synth_one(markdown, tmp_path / "business" / "business.mp3", "us_female", "-10%"))

    spoken = state["calls"][0].text
    assert out.read_bytes() == MP3_PROBE
    assert state["calls"][0].voice == "en-US-AriaNeural" and state["calls"][0].rate == "-10%"
    for marker in ("#", "**", "|", "](http", "<details", "```"):
        assert marker not in spoken, marker
    titles = [str(block.get("title")) for block in (pack.get("levels") or {}).values() if isinstance(block, dict)]
    assert any(spoken.startswith(title) for title in titles)  # 标题就是口播稿的开头
    assert "Regulators in the European Union have fined" in spoken
    assert len(speech_text(markdown).split()) < len(markdown.split())  # 确实削掉了标记与 URL


# ---------------------------------------------------------------- 命令行
def test_cli_single_file_with_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_edge_tts(monkeypatch)
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = tts_main(
            [
                "--text",
                "Hello from EnglishFoundry.",
                "--out",
                str(tmp_path / "audio" / "hello.mp3"),
                "--voice",
                "us_male",
                "--rate=-10%",
                "--manifest",
            ]
        )

    assert code == 0
    assert (tmp_path / "audio" / "hello.mp3").read_bytes() == MP3_PROBE
    assert (tmp_path / "audio" / "manifest.json").is_file()  # --out 时清单跟着落在同一目录
    assert call_for(state, "Hello from EnglishFoundry").voice == "en-US-GuyNeural"
    assert call_for(state, "Hello from EnglishFoundry").rate == "-10%"
    assert "1/1 成功" in buffer.getvalue()


def test_cli_reads_files_and_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "posts"
    source.mkdir()
    (source / "story.md").write_text(MARKDOWN, encoding="utf-8")
    (source / "note.txt").write_text("Plain note text.", encoding="utf-8")
    state = install_edge_tts(monkeypatch)
    out_dir = tmp_path / "audio"

    assert tts_main(["--file", str(source / "story.md"), "--file", str(source / "note.txt"), "--out-dir", str(out_dir)]) == 0
    assert sorted(path.name for path in out_dir.glob("*.mp3")) == ["note.mp3", "story.mp3"]
    assert call_for(state, "EU fines hit").text == speech_text(MARKDOWN)
    assert call_for(state, "Plain note text").text == "Plain note text."
    assert tts_main(["--file", str(source / "story.md"), "--out-dir", str(out_dir), "--skip-existing"]) == 0
    assert tts_main(["--file", str(source / "missing.md"), "--out-dir", str(out_dir)]) == 1
    assert tts_main([]) == 2
    assert tts_main(["--text", "one", "--text", "two", "--out", str(out_dir / "only-one.mp3")]) == 2


def test_cli_reports_failures_but_keeps_going(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch, fail_on=lambda text: RuntimeError("boom") if "boom" in text else None)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = tts_main(
            ["--text", "Good sentence here.", "--text", "This one goes boom.", "--out-dir", str(tmp_path / "audio"), "--workers", "1"]
        )

    assert code == 1
    printed = buffer.getvalue()
    assert "1/2 成功" in printed and "FAIL" in printed and "RuntimeError" in printed
    assert (tmp_path / "audio" / "clip-01.mp3").is_file()


def test_cli_fixes_wrong_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_edge_tts(monkeypatch)
    assert tts_main(["--text", "Suffix check.", "--out", str(tmp_path / "audio" / "clip.wav")]) == 0
    assert (tmp_path / "audio" / "clip.mp3").is_file()


def test_cli_list_voices_shows_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert tts_main(["--list-voices"]) == 0
    printed = buffer.getvalue()
    for alias, name in VOICES.items():
        assert alias in printed and name in printed


# ---------------------------------------------------------------- 联网验收（默认跳过）
@pytest.mark.skipif(not LIVE, reason="需要联网：FOUNDRY_TTS_LIVE=1 pytest tests/test_tts.py -k live")
def test_live_edge_tts_makes_playable_mp3(tmp_path: Path) -> None:  # pragma: no cover - 需要联网
    if tts.edge_tts is None:
        raise AssertionError('先安装 edge-tts：pip install "edge-tts>=6.1.0"')
    results = batch_synth(
        {
            "aria": "Hello! This is a live acceptance test of the EnglishFoundry text to speech module.",
            "guy": "Guy reads this second sentence too.",
        },
        tmp_path,
        attempts=2,
    )
    for row in results:
        assert row.ok, row.error
        payload = row.path.read_bytes()
        assert len(payload) > 1000
        assert payload[:3] == b"ID3" or (payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0), payload[:8]

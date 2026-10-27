任务：实现免费配音 src/foundry/tts.py（edge-tts，微软，免费、可联网）。
要求：
- 用 edge-tts 异步接口，把文本合成 mp3；可选 voice（默认 en-US-AriaNeural，另提供 en-US-GuyNeural、en-GB-SoniaNeural、en-AU-NatashaNeural）、rate（默认 +0%）。
- 提供 async synth_one(text, out_path, voice, rate) 与同步包装 batch_synth(items, out_dir)。
- 输出路径与失败隔离（单条失败记录并继续）。
验收：在能联网环境为一段英文生成非空 mp3；写 tests/test_tts.py，用 monkeypatch 替换 edge_tts.Communicate 使其离线可测。

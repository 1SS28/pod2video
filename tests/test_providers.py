"""Transcription parsers, the LLM wrapper, and feed parsing, with no network calls."""

import json
from types import SimpleNamespace

import httpx

from core.llm import LLM
from pod2video.feed import parse_feed, pick_episode
from pod2video.transcribe import AssemblyAI, Deepgram


def test_assemblyai_flow_and_word_normalisation(tmp_path):
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"mp3")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": "https://cdn.assemblyai.com/upload/1"})
        if request.url.path == "/v2/transcript":
            body = json.loads(request.content)
            assert body["speaker_labels"] is True and body["audio_url"].endswith("/upload/1")
            return httpx.Response(200, json={"id": "t1", "status": "queued"})
        return httpx.Response(200, json={
            "status": "completed", "audio_duration": 12.5, "language_code": "en",
            "words": [
                {"text": "Hello,", "start": 100, "end": 400, "confidence": 0.9, "speaker": "A"},
                {"text": "world.", "start": 450, "end": 900, "confidence": 0.95, "speaker": "B"},
            ],
        })

    words, duration, lang = AssemblyAI("key", poll_interval=0, transport=httpx.MockTransport(handler)).transcribe(audio)
    assert [(w.text, w.start, w.end, w.speaker) for w in words] == [("Hello,", 0.1, 0.4, "A"), ("world.", 0.45, 0.9, "B")]
    assert (duration, lang) == (12.5, "en")
    assert seen[0].headers["authorization"] == "key"


def test_deepgram_speakers_become_letters(tmp_path):
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"mp3")

    def handler(request):
        assert request.url.params["diarize"] == "true"
        assert request.headers["authorization"] == "Token key"
        return httpx.Response(200, json={
            "metadata": {"duration": 3.0},
            "results": {"channels": [{"detected_language": "en", "alternatives": [{"words": [
                {"word": "hi", "punctuated_word": "Hi.", "start": 0.1, "end": 0.3, "speaker": 0},
                {"word": "yo", "punctuated_word": "Yo!", "start": 0.5, "end": 0.7, "speaker": 1},
            ]}]}]},
        })

    words, duration, _ = Deepgram("key", transport=httpx.MockTransport(handler)).transcribe(audio)
    assert [(w.text, w.speaker) for w in words] == [("Hi.", "A"), ("Yo!", "B")]
    assert duration == 3.0


def _fake_anthropic_response(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=2000, output_tokens=300),
    )


def test_llm_anthropic_structured_output_and_cost():
    calls = {}

    class Messages:
        def create(self, **kwargs):
            calls["plain"] = kwargs
            return _fake_anthropic_response('{"ok": true}')

    class BetaMessages:
        def create(self, **kwargs):
            calls["beta"] = kwargs
            return _fake_anthropic_response('{"ok": true}')

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    llm = LLM("claude-opus-5-5", "key")
    llm._client = SimpleNamespace(messages=Messages(), beta=SimpleNamespace(messages=BetaMessages()))
    result = llm.json(system="s", prompt="p", schema=schema)
    assert result.data == {"ok": True}
    assert calls["beta"]["output_config"]["format"]["schema"] == schema
    assert calls["beta"]["fallbacks"] == "default"
    assert result.usd == (2000 * 4 + 300 * 20) / 1_000_000

    haiku = LLM("claude-haiku-4-5", "key")
    haiku._client = SimpleNamespace(messages=Messages(), beta=SimpleNamespace(messages=BetaMessages()))
    haiku.json(system="s", prompt="p", schema=schema)
    assert "fallbacks" not in calls["plain"]


RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>Side Project Radio</title><itunes:image href="https://example.com/art.jpg"/>
<item><title>Episode 1: Origins</title><guid>ep1</guid><pubDate>Mon, 01 Sep 2025 10:00:00 GMT</pubDate>
<enclosure url="https://example.com/ep1.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Episode 2: Shipping</title><guid>ep2</guid><pubDate>Mon, 08 Sep 2025 10:00:00 GMT</pubDate>
<enclosure url="https://example.com/ep2.mp3" type="audio/mpeg" length="1"/></item>
</channel></rss>"""


def test_feed_parsing_and_episode_selection():
    episodes = parse_feed(RSS, "https://example.com/feed.xml")
    assert [e.guid for e in episodes] == ["ep2", "ep1"]  # newest first
    assert episodes[0].podcast_title == "Side Project Radio"
    assert episodes[0].podcast_image == "https://example.com/art.jpg"
    assert pick_episode(episodes, "latest").guid == "ep2"
    assert pick_episode(episodes, "2").guid == "ep1"
    assert pick_episode(episodes, "origins").guid == "ep1"
    assert pick_episode(episodes, "ep2").title == "Episode 2: Shipping"

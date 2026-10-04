from core.costs import CostLedger
from core.llm import LLMResult
from pod2video import moments as m


def test_sentences_split_on_punctuation_and_speaker(transcript):
    sentences = transcript.sentences()
    texts = [s.text for s in sentences]
    assert texts[0] == "Welcome to the show."
    assert "Here is the uncomfortable truth." in texts
    assert all(s.speaker in ("A", "B") for s in sentences)
    # Sentences tile the words with no gaps or overlaps.
    assert sentences[0].first_word == 0
    for prev, nxt in zip(sentences, sentences[1:]):
        assert nxt.first_word == prev.last_word + 1
    assert sentences[-1].last_word == len(transcript.words) - 1


def test_fit_window_extends_and_trims(transcript):
    sentences = transcript.sentences()
    first, last = m.fit_window(sentences, 2, 2, min_len=8, max_len=12)
    length = sentences[last].end - sentences[first].start
    assert 8 <= length <= 12
    first, last = m.fit_window(sentences, 0, len(sentences) - 1, min_len=1, max_len=6)
    assert first == 0 and sentences[last].end - sentences[0].start <= 6


def test_moments_from_ranges_drops_overlaps_and_pads(transcript):
    sentences = transcript.sentences()
    candidates = [
        {"start_sentence": 2, "end_sentence": 3, "title": "Truth", "hook": "", "reason": ""},
        {"start_sentence": 3, "end_sentence": 4, "title": "Overlaps", "hook": "", "reason": ""},
        {"start_sentence": 6, "end_sentence": 8, "title": "Ship it", "hook": "", "reason": ""},
    ]
    chosen = m.moments_from_ranges(transcript, sentences, candidates, count=2, min_len=3, max_len=20)
    assert [c.title for c in chosen] == ["Truth", "Ship it"]
    truth = chosen[0]
    first_word = transcript.words[sentences[2].first_word]
    assert first_word.start - m.LEAD_IN <= truth.start <= first_word.start
    assert truth.end >= transcript.words[sentences[3].last_word].end


def test_select_moments_uses_llm_and_logs_cost(transcript):
    class FakeLLM:
        model = "fake-model"

        def json(self, *, system, prompt, schema, name):
            assert "[S2 " in prompt and "Bob" in prompt
            return LLMResult(
                data={"moments": [{"start_sentence": 2, "end_sentence": 3, "title": "Truth", "hook": "h", "reason": "r"}]},
                provider="fake", model="fake-model", input_tokens=1000, output_tokens=100, usd=0.01,
            )

    ledger = CostLedger()
    chosen = m.select_moments(
        transcript, FakeLLM(), count=1, min_len=3, max_len=20, speaker_names={"A": "Alice", "B": "Bob"},
        podcast="Pod", episode="Ep", ledger=ledger,
    )
    assert len(chosen) == 1 and chosen[0].title == "Truth"
    assert ledger.totals()["llm_usd"] == 0.01


def test_snap_to_words(transcript):
    w = transcript.words
    start, end = m.snap_to_words(transcript, w[3].start + 0.1, w[8].end - 0.1)
    # A word cut in half at the start is dropped; the end extends to finish the word in progress.
    assert start == round(w[4].start - m.LEAD_IN, 3)
    assert end >= w[7].end


def test_schema_is_strict_mode_compatible():
    def check(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                check(child)
        if node.get("type") == "array":
            check(node["items"])

    check(m.SCHEMA)

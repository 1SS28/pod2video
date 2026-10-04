import math
import wave
from array import array

from pod2video import audio


def write_tone(path, seconds=2.0, rate=8000):
    samples = array("h", (int(10000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate))))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def read(path):
    with wave.open(str(path), "rb") as w:
        data = array("h")
        data.frombytes(w.readframes(w.getnframes()))
        return data, w.getframerate()


def test_isolate_keeps_only_intervals_and_preserves_length(tmp_path):
    src, dest = tmp_path / "in.wav", tmp_path / "out.wav"
    write_tone(src)
    audio.isolate(src, [(0.5, 1.0)], dest)
    data, rate = read(dest)
    original, _ = read(src)
    assert len(data) == len(original)
    assert all(v == 0 for v in data[: int(0.5 * rate)])
    assert all(v == 0 for v in data[int(1.0 * rate) :])
    middle = data[int(0.6 * rate) : int(0.9 * rate)]
    assert max(abs(v) for v in middle) > 9000


def test_speaker_intervals_are_clip_relative_and_merged(transcript):
    words = transcript.words
    start, end = words[0].start, words[-1].end
    spans = audio.speaker_intervals(words, "B", clip_start=start, clip_end=end, merge_gap=0.35)
    b_words = [w for w in words if w.speaker == "B"]
    assert spans[0][0] <= b_words[0].start - start
    assert spans[-1][1] >= b_words[-1].end - start
    # B speaks in two separate turns.
    assert len(spans) == 2


def test_shot_list_cuts_to_the_speaker_and_ignores_interjections():
    alice = [(0.0, 5.0), (12.0, 20.0)]
    bob = [(5.2, 11.8), (14.0, 14.3)]  # a long turn, then a short "yeah"
    shots = audio.shot_list([alice, bob], 20.0)
    assert shots[1] == [(5.2, 12.0)]
    assert shots[0] == [(0.0, 5.2), (12.0, 20.0)]


def test_shot_list_respects_minimum_shot_length():
    alice = [(0.0, 0.5)]
    bob = [(0.6, 10.0)]
    shots = audio.shot_list([alice, bob], 10.0, min_shot=1.2)
    assert shots[0] == [(0.0, 1.2)]
    assert shots[1] == [(1.2, 10.0)]


def test_condense_joins_spans_with_pads_at_the_reported_offsets(tmp_path):
    src, dest = tmp_path / "in.wav", tmp_path / "out.wav"
    write_tone(src, seconds=4.0)
    spans = [(0.5, 1.0), (2.0, 3.0)]
    audio.condense(src, spans, dest, pad=0.25)
    data, rate = read(dest)
    assert len(data) == round((0.5 + 0.25 + 1.0) * rate)
    assert audio.span_offsets(spans, 0.25) == [0.0, 0.75]
    gap = data[int(0.52 * rate) : int(0.73 * rate)]
    assert all(v == 0 for v in gap)

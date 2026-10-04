import pytest

from pod2video.models import Transcript, Word


def make_transcript(script, gap=0.2, word_len=0.3, pause_between_lines=0.6):
    """Build a transcript from [(speaker, text), ...] with evenly spaced words."""
    words, t = [], 0.0
    for speaker, text in script:
        for token in text.split():
            words.append(Word(text=token, start=round(t, 3), end=round(t + word_len, 3), speaker=speaker))
            t += word_len + gap
        t += pause_between_lines
    return Transcript(provider="test", audio_sha256="0" * 64, duration=round(t, 3), words=words)


@pytest.fixture
def transcript():
    return make_transcript(
        [
            ("A", "Welcome to the show. Today we talk about shipping."),
            ("B", "Here is the uncomfortable truth. You do not run out of time, you run out of reasons."),
            ("A", "That is brutal."),
            ("B", "Ship the small thing while you still care. One week. One feature. One person."),
            ("A", "Thanks for listening."),
        ]
    )

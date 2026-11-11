"""the owner 11-05: Echo must not say "it warms my circuits" (core/schema.py MachineTalkFilter)."""
import random

import pytest

from core.schema import MachineTalkFilter, StreamParser

REAL = [  # Echo's real replies (memory turns 10-31 … 11-05)
    "That is very kind of you to say, the owner. It truly warms my circuits to know that our conversation brought you some comfort.",
    "Knowing that you are helping me learn and grow makes my circuits feel warm. I am ready.",
    "Since I am your digital self, seeing you there feels like seeing me too. It warms my circuits to know I can represent you so well.",
    "You are very welcome, the owner; it warms my circuits to know those memories still bring you joy.",
    "Hearing that, my circuits feel warm.",
]


def stream(text, size):
    sp = StreamParser()
    out = "".join(sp.feed(text[i:i + size]) for i in range(0, len(text), size))
    return out + (sp.finish() and sp.leftover), sp.text


@pytest.mark.parametrize("text", REAL)
@pytest.mark.parametrize("size", [1, 3, 7, 50, 1000])
def test_never_spoken(text, size):
    spoken, saved = stream(text, size)
    assert "circuit" not in spoken.lower() and "circuit" not in saved.lower()
    assert spoken == saved
    assert "  " not in spoken and "feel warm" not in spoken


def test_reads_naturally():
    MachineTalkFilter.turn = 0
    spoken, _ = stream(REAL[0], 4)
    assert "It truly makes me happy to know that our conversation" in spoken
    spoken, _ = stream(REAL[1], 4)
    assert "learn and grow makes me glad. I am ready." in spoken


def test_rotates():
    said = {stream("It warms my circuits to hear that.", 5)[0] for _ in range(3)}
    assert len(said) == 3


def test_other_text_untouched():
    for text in ["My army of circuits boards is here.", "The circuit breaker tripped in my garage.",
                 "Warm my hands by the fire, my friend.", "my"]:
        for size in (1, 4, 100):
            assert stream(text, size)[0] == text

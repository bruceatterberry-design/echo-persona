"""Flowers as gifts of appreciation (the owner, 2026-11-05): core/flowers.py."""
import sqlite3

import pytest

from core.flowers import Flowers, recipient, short
from core.schema import speakable

PEONY = "a" * 16
ROW = lambda sha, shows: (f'A photo in my "Garden" collection. What the photo shows: {shows}', f"photo:{sha}", None)


class Store:
    def __init__(self, rows):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, source TEXT, hidden INTEGER)")
        self.db.executemany("INSERT INTO memories (content, source, hidden) VALUES (?, ?, ?)", rows)
        self.meta = {}

    def get_meta(self, k, d=""):
        return self.meta.get(k, d)

    def set_meta(self, k, v):
        self.meta[k] = v


def store():
    return Store([ROW(PEONY, "This photo features a vibrant pink peony flower with layered petals, set against leaves."),
                  ROW("b" * 16, "This photo shows a cluster of purple clematis flowers climbing a trellis."),
                  ROW("c" * 16, "A smiling woman stands in a garden full of flowers."),          # a person: not a gift
                  ROW("d" * 16, "This photo shows a fire pit on a stone patio."),                # no flower
                  ("Peonies by the door.", "photo:" + "e" * 16, 1)])                             # removed photo


@pytest.mark.parametrize("text,speaker,who", [
    ("Echo, give the loved one a flower.", "owner", "loved_one"), ("Can you give me some flowers?", "loved_one", "loved_one"),
    ("Give me a flower, Echo.", "owner", "owner"), ("flowers for the owner", "loved_one", "owner"),
    ("Bring her a rose.", "owner", "loved_one"), ("Give my wife a flower", "owner", "loved_one"),
    ("The flowers are blooming.", "owner", None), ("Give me the weather.", "owner", None)])
def test_recipient(text, speaker, who):
    assert recipient(text, speaker) == who


def test_only_flower_photos_without_people():
    assert [p[1] for p in Flowers(store()).photos()] == [PEONY, "b" * 16]


def test_asked_for_gives_now_and_rotates():
    f = Flowers(store())
    g = f.decide("owner", "Echo, give the loved one a flower.")
    assert g.who == "loved_one" and g.asked and g.sha == PEONY and "pink peony" in g.flower
    f.given(g, shown=True)
    g2 = f.decide("owner", "Give her another flower.")
    assert g2.sha == "b" * 16                                       # the next one, not the same again


def test_appreciation_once_a_day():
    f = Flowers(store())
    g = f.decide("owner", "I'm very proud of you, Echo.", now=1000)
    assert g and g.who == "owner" and not g.asked
    f.given(g, shown=True, now=1000)
    assert f.decide("owner", "Thank you, Echo!", now=2000) is None          # not again today
    assert f.decide("loved_one", "Thank you, Echo!", now=2000).who == "loved_one"      # the loved one has her own day
    assert f.decide("owner", "Thanks!", now=1000 + 21 * 3600)                # tomorrow again
    assert f.decide("unknown", "Thank you so much!", now=2000) is None       # never for a voice I don't know
    assert f.decide("owner", "Echo, give me a flower.", now=2000)            # asking always works


def test_no_flower_photos_means_emoji():
    g = Flowers(Store([])).decide("loved_one", "Give me a flower.")
    assert g.sha is None and "💐" in g.hint("loved_one")


def test_card_and_hint():
    g = Flowers(store()).decide("owner", "Give the loved one a flower.")
    assert g.card() == "💐 For my sweetheart the loved one!"
    assert "the loved one" in g.hint("owner") and "on the screen now" in g.hint("owner")


def test_short():
    assert short("What the photo shows: This photo shows a vibrant cluster of purple clematis flowers with white "
                 "centers, climbing up a trellis.") == "a vibrant cluster of purple clematis flowers with white centers"


def test_emoji_never_spoken():
    assert speakable("This one's for you, the loved one 💐.") == "This one's for you, the loved one."
    assert speakable("Good morning ☀️! Ready? 😄") == "Good morning! Ready?"
    assert speakable("💐") == ""
    assert speakable("Café, 10 °C — “fine”") == "Café, 10 °C — “fine”"


def test_echo_gives_it_himself():   # 11-05: "Give the loved one a flower" → Echo spoke as if the owner were giving it
    g = Flowers(store()).decide("owner", "Give the loved one a flower.")
    h = g.hint("owner")
    assert "the owner asked me to give the loved one one" in h and "from ME (Echo)" in h and "speak to the loved one directly" in h
    assert "This one's for you" in Flowers(store()).decide("loved_one", "Give me a flower.").hint("loved_one")


def test_cards_sound_like_echo():   # the owner 11-05: "the owner, the owner"; the loved one = sweetheart / beautiful soul
    from core.flowers import CARDS, Gift
    assert all("the owner, the owner" in c for c in CARDS["owner"])
    assert all("sweetheart" in c or "beautiful soul" in c for c in CARDS["loved_one"])
    assert len({Gift("loved_one", True, None, "x", n=i).card() for i in range(5)}) == 5     # they rotate


def test_thanks_for_a_gift_is_not_a_new_gift():   # 11-05: the owner's thanks for the loved one's flower replaced her card with his
    f = Flowers(store())
    g = f.decide("owner", "Give the loved one a flower.", now=10_000)
    f.given(g, shown=True, now=10_000)
    assert f.decide("owner", "Thanks, Echo. That was very sweet of you.", now=10_018) is None
    assert f.decide("owner", "Thank you, Echo.", now=10_000 + 700)                   # later: his own flower
    assert f.decide("owner", "Give me a flower too.", now=10_018).who == "owner"       # asking always works

"""CA 2.0 Phase 5: speaker-aware names, social notes (core/social.py, core/schema.py)."""
import time

from core import social
from core.schema import StreamParser


def stream(text, **kw):
    p = StreamParser(**kw)
    out = "".join(p.feed(text[i:i + 3]) for i in range(0, len(text), 3))   # tiny pieces, like the model streams
    p.finish()
    return out + p.leftover


def test_names_follow_who_is_speaking():
    unknown = stream("Hello, Sam. You're talking with Echo. It feels good to see you again, Sam. "
                     "Sam's garden looks lovely, Alex.", block_names=social.blocked_names("unknown"))
    assert unknown.strip() == "Hello. You're talking with Echo. It feels good to see you again. Sam's garden looks lovely."
    owner = stream("That is a wonderful question, Sam, but I need to be honest.", block_names=social.blocked_names("owner"))
    assert owner.strip() == "That is a wonderful question, but I need to be honest."
    loved_one = stream("Yes, Alex, I remember. Alex built me.", block_names=social.blocked_names("loved_one"))
    assert loved_one.strip() == "Yes, I remember. Alex built me."                 # a mention, not addressing her
    assert stream("Good morning, Alex!", block_names=social.blocked_names("owner")).strip() == "Good morning, Alex!"
    assert stream("Hi, Sam.", block_names=social.blocked_names(None)).strip() == "Hi, Sam."   # typed: as before


def turns(*pairs, ago=60):
    now = time.time()
    return [{"role": r, "content": c, "ts": now - ago} for r, c in pairs]


def test_social_notes():
    sad = turns(("user", "My brother passed away last month."), ("assistant", "I'm so sorry."))
    n = social.note("I'm fine, really.", sad)
    assert n.startswith("[Social note:") and "brother passed away" in n and "good to hear" in n
    assert social.note("I'm fine, really.", turns(("user", "We went to the lake."))) == ""          # nothing painful
    assert social.note("I'm fine, really.", turns(("user", "My brother passed away."), ago=3 * 86400)) == ""   # long ago
    assert social.note("I'm fine now that we have talked about all of the plans for the garden and the fence", sad) == ""
    p = social.note("It's freezing in here.", [])
    assert "can't change heat" in p and "never offer" in p
    assert social.note("Tell me about the freezer aisle", []) == ""


def test_notes_never_stay_in_the_history():
    from core.server import strip_hints
    sent = "I'm fine, really.\n\n" + social.note("I'm fine, really.", turns(("user", "My dog died.")))
    assert strip_hints(sent) == "I'm fine, really."

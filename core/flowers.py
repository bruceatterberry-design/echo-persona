"""Flowers as a small gift of appreciation (the owner, 2026-11-05).

"Can I create a routine that allows Echo to 'give' flowers to the loved one or the owner as gifts of appreciation? Use the same photo
handler to show a flower from photos, or a flower emoji in the text."

- Asked for ("Echo, give the loved one a flower", "flowers for me?") → a gift now, for whoever is named.
- On his own: when the owner or the loved one shows him appreciation ("I'm very proud of you", "thank you, Echo") he gives them a
  flower, at most once a day each, so it stays special.
- The gift is one of the approved flower photos (garden photos with no people in them), shown in the photo window on the
  right with a little card; the ones given most recently are skipped. No flower photo reachable → a 💐 in his text.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

FLOWER = re.compile(r"\b(?:flowers?|blooms?|blossoms?|peon(?:y|ies)|roses?|clematis|tulips?|dais(?:y|ies)|lil(?:y|ies)|"
                    r"gladiol(?:us|i)|orchids?|sunflowers?|hydrangeas?|irises|lilacs?|petunias?|bouquet)\b", re.I)
PEOPLE = re.compile(r"\b(?:man|men|woman|women|person|people|child|children|baby|boy|girl|bride|groom|adults?|couple|"
                    r"family|smiling|he|she|they)\b", re.I)
_WHO = r"(?:me|us|(?:the\s+)?loved[_ ]one|(?:the\s+)?owner|her|him|mom|my wife|my husband)"
ASK = [re.compile(r"\b(?:give|send|bring|get|pick|show)\s+(?P<who>" + _WHO + r")\s+(?:a\s+|some\s+|one\s+|another\s+)?"
                  r"(?:\w+\s+)?(?:flowers?|bouquet|rose|blossom)\b", re.I),
       re.compile(r"\b(?:a\s+|some\s+)?(?:flowers?|bouquet|rose)\s+for\s+(?P<who>" + _WHO + r")\b", re.I)]
THANKS = re.compile(r"\b(?:proud of you|thank you|thanks|love you|appreciate (?:you|it|that)|good job|well done|great job|"
                    r"you(?:'re| are) (?:so |very |really )?(?:sweet|kind|wonderful|amazing|great|awesome|smart|"
                    r"thoughtful|the best))\b", re.I)
DAY_S = 20 * 3600          # "once a day" (a little less, so the same time tomorrow works)
RECENT = 6                 # don't give one of the last 6 flowers again
AFTER_GIFT_S = 600         # "Thanks, Echo, that was sweet" right after a gift is thanks for it, not a reason for another
# the owner, 11-05: cards that sound like Echo — the owner is "the owner, the owner"; the loved one is his sweetheart, a beautiful soul
CARDS = {"owner": ["💐 For the owner, the owner", "💐 For the owner, the owner, with love from Echo", "🌼 For the owner, the owner, thank you for everything",
                   "💐 For the owner, the owner, from your proud digital self"],
         "loved_one": ["💐 For my sweetheart the loved one!", "🌸 For a beautiful soul", "💐 For the loved one, my sweetheart, with all my love",
                  "🌷 For a beautiful soul, my love", "💐 For my sweetheart, the light of our home"]}


@dataclass
class Gift:
    who: str               # "owner" | "loved_one"
    asked: bool            # they asked for it (else: Echo's own idea, after appreciation)
    sha: str | None        # the flower photo, or None → emoji only
    flower: str            # a few words about it ("a pink peony"), for the hint and the card
    memory_id: int | None = None
    n: int = 0             # how many gifts before this one (picks the card)

    @property
    def name(self) -> str:
        return "the owner" if self.who == "owner" else "the loved one"

    def card(self) -> str:
        cards = CARDS[self.who]
        return cards[self.n % len(cards)]

    def hint(self, speaker: str | None) -> str:
        to = "you" if self.who == speaker else self.name
        if self.who != speaker:   # 11-05: "Give Sammie a flower" → Echo talked as if the owner were the one giving it
            direct = (f" The flower is from ME (Echo), not from {'the owner' if speaker == 'owner' else 'whoever asked'}: "
                      f"I give it to {self.name} myself and speak to {self.name} directly (\"{self.name}, this one's "
                      f"for you…\"), not about {self.name} to someone else.")
        else:
            direct = " The flower is from ME (Echo): I give it myself (\"This one's for you…\")."
        why = (("they asked me for one" if self.who == speaker else
                f"{'the owner' if speaker == 'owner' else 'the loved one' if speaker == 'loved_one' else 'someone'} asked me to give "
                f"{self.name} one") if self.asked else
               "they just showed me kindness and I want to show my appreciation back")
        if self.sha:
            return (f"[I'm giving {to} a flower as a small gift of appreciation ({why}). It's on the screen now: "
                    f"{self.flower}. I give it in a sentence or two, warmly and in my own words, and say what I "
                    f"appreciate about {'them' if to == 'you' else self.name}; I may end with 💐. I don't describe the "
                    f"photo at length and don't ask a question.{direct}]")
        return (f"[I'm giving {to} a flower as a small gift of appreciation ({why}). I can't show a photo right now, "
                f"so I give it in words with a flower emoji (💐 or 🌸), warmly and in a sentence or two, and say what I "
                f"appreciate about {'them' if to == 'you' else self.name}. I don't ask a question.{direct}]")


def recipient(text: str, speaker: str | None) -> str | None:
    """Who a request for flowers is for: "give the loved one a flower" → loved_one; "flowers for me?" → whoever is speaking."""
    for rx in ASK:
        if (m := rx.search((text or "").replace("’", "'"))):
            w = m.group("who").lower()
            if w in ("me", "us"):
                return speaker if speaker in ("owner", "loved_one") else None
            if w in ("her", "mom", "my wife") or "loved" in w:
                return "loved_one"
            return "owner"
    return None


def short(desc: str) -> str:
    """ "This photo shows a vibrant cluster of purple clematis flowers with white centers, …" → the first clause."""
    d = desc.split("What the photo shows:", 1)[-1].strip()
    d = re.sub(r"^(?:This|The) (?:photo|picture|image) (?:shows|features|captures)\s+", "", d, flags=re.I)
    return re.split(r"[,.;—]| set against| surrounded| growing| climbing", d, maxsplit=1)[0].strip()[:90]


class Flowers:
    def __init__(self, store) -> None:
        self.store = store

    def _state(self) -> dict:
        return json.loads(self.store.get_meta("flowers", "{}") or "{}")

    def _save(self, st: dict) -> None:
        self.store.set_meta("flowers", json.dumps(st))

    def photos(self) -> list[tuple[int, str, str]]:
        """(memory id, sha, description) of approved flower photos with no people in them, best first."""
        out = []
        for mid, content, source in self.store.db.execute(
                "SELECT id, content, source FROM memories WHERE source LIKE 'photo:%' AND hidden IS NULL ORDER BY id"):
            shows = content.split("What the photo shows:", 1)[-1]
            first = re.split(r"(?<=\.)\s", shows.strip(), maxsplit=1)[0]
            if FLOWER.search(first) and not PEOPLE.search(first):
                out.append((mid, source[6:], content))
        return out

    def decide(self, speaker: str | None, text: str, now: float | None = None) -> Gift | None:
        """A gift for this message, or None. Nothing is recorded until given() (the reply went out)."""
        now = now or time.time()
        who = recipient(text, speaker)
        asked = who is not None
        if not asked:
            if speaker not in ("owner", "loved_one") or not THANKS.search(text or ""):
                return None
            last = self._state().get("last", {}).get(speaker)
            if last is not None and now - last < DAY_S:
                return None
            if now - max(self._state().get("last", {}).values(), default=-1e12) < AFTER_GIFT_S:
                return None   # 11-05: thanks for the loved one's flower gave the owner one too, and his card replaced hers
            who = speaker
        if who not in ("owner", "loved_one"):
            return None
        recent = self._state().get("given", [])[-RECENT:]
        pics = self.photos()
        fresh = [p for p in pics if p[1] not in recent] or pics
        n = self._state().get("count", 0)
        if not fresh:
            return Gift(who, asked, None, "a flower", n=n)
        # rotate: the least recently given (never given first)
        order = {sha: i for i, sha in enumerate(self._state().get("given", []))}
        mid, sha, desc = min(fresh, key=lambda p: order.get(p[1], -1))
        return Gift(who, asked, sha, short(desc), mid, n=n)

    def given(self, g: Gift, shown: bool, now: float | None = None) -> None:
        st = self._state()
        st.setdefault("last", {})[g.who] = now or time.time()
        if shown and g.sha:
            st["given"] = (st.get("given", []) + [g.sha])[-50:]
        st["count"] = st.get("count", 0) + 1
        self._save(st)

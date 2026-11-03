"""Initiative (CA 2.0 Phase 9): what is worth asking the owner about next, with the reason — plain rules, no model call.

Directive §8: an appraisal from measurable signals (knowledge gaps, goal relevance, unfinished investigations, user
interests), used to choose what deserves attention, and Echo asks the owner whether they'd like to explore it. Scores are
bookkeeping, not feelings.

Measured 11-03: 108 of Echo's 121 photo memories have no caption from the owner, so Echo can't say who or what is in them
(Phase 6: "which of my cats is on the cat tree?" → he guessed). Until now his questions came only from the fixed
life-question bank. Now the same question slot (memory/curiosity.py: the owner's voice only, at most every 30 minutes,
or when they say "ask me something") alternates: a photo question, then a bank question. the owner-approved learning
objectives still come first. His answer becomes memory *suggestions* to review, exactly like the bank questions.

Signals for a photo (all from the photo memory's own text):
  people_unnamed  people in the description and no caption: names are the biggest gap        +3
  pet_unnamed     a cat/dog and no caption (Phase 6: which cat is which)                      +2
  dated           the camera date is known, so the question can say when                       +1
  part_of_a_day   other photos were taken the same day: one answer may explain several        +1
"""
from __future__ import annotations

import re
import time

PEOPLE = re.compile(r"\b(?:man|men|woman|women|boy|boys|girl|girls|child|children|kids?|baby|people|person|person’s|"
                    r"person's|adults?|family|couple|group|friends?|bride|groom|selfie|firefighter|someone|toddlers?|infants?|newborn|"
                    r"daughters?|sons?|teenagers?|grandparents?|portrait)\b", re.I)
PET = re.compile(r"\b(?:cat|cats|kitten|kitty|tabby|dog|dogs|puppy)\b", re.I)
TAKEN = re.compile(r"Taken on (\d{1,2} \w+ \d{4})")
SHOWS = re.compile(r"What the photo shows:\s*(?:This (?:photo|image|picture) (?:shows|captures|features|depicts)\s*)?([^.]+)",
                   re.I)
COLLECTION = re.compile(r'A photo in my "([^"]+)" collection')


DANGLING = {"a", "an", "the", "and", "with", "of", "in", "on", "at", "by", "while", "his", "her", "their", "its", "to",
            "against", "under", "near", "next", "from", "into", "onto", "over", "beside", "inside", "outside",
            "white", "black", "pink", "orange", "brown", "gray", "grey", "red", "blue", "green", "yellow", "purple",
            "light", "dark", "cozy", "small", "large", "big", "little", "old", "young"}
VERB = re.compile(r"^(?:sits|sitting|stands|standing|lies|lying|rests|resting|holds|holding|smiles|smiling|poses|posing|"
                  r"grows|growing|leans|leaning|looks|looking|wearing|is|are|sit|stand|lie|rest|hold)$", re.I)


def _label(text: str, words: int = 10) -> str:
    """A short spoken description: "a man in a blue office chair holding a newborn baby" — the first phrase of the
    description, the "this photo shows" part dropped, never cut mid-phrase."""
    m = SHOWS.search(text)
    t = (m[1] if m else "").strip()
    t = re.sub(r"^(?:an? |this |the )?(?:close-up |nighttime |vintage |candid |black-and-white |old )?(?:family )?"
               r"(?:selfie|photo|picture|image|snapshot|portrait|photograph)\s+(?:shows|features|captures|of|with|is)\s+",
               "", t, flags=re.I)
    full = t
    t = re.split(r"[;—:(]|,(?! (?:and )?(?:white|black|orange|brown|gray|grey|red|blue|green|yellow|pink|purple)\b)"
                 r"| while | which | that ", t)[0]
    if len(t.split()) < 3:   # "a large, old barn…": the first phrase is too short alone
        t = re.split(r"[;—:(]| while | which | that ", full)[0]
    w = t.split()[:words]
    while w and (w[-1].lower().strip("’',") in DANGLING or VERB.match(w[-1].strip(","))):
        w.pop()
    if not w:
        return ""
    if w[0] in ("A", "An", "The", "Two", "Three", "This"):
        w[0] = w[0].lower()
    return " ".join(w)


def _month_year(date: str) -> str:
    try:
        return time.strftime("%B %Y", time.strptime(date, "%d %B %Y"))
    except ValueError:
        return date


def photo_question(mid: int, text: str) -> dict | None:
    """The question for one uncaptioned photo memory, with its score and reasons (None if it has a caption)."""
    if re.search(r"\bthe owner says:", text, re.I) or not (lab := _label(text)):
        return None
    why, score = [], 0
    desc = (m[1] if (m := SHOWS.search(text)) else "")
    people, pet = bool(PEOPLE.search(desc)), bool(PET.search(desc))
    if people:
        why.append("people_unnamed"); score += 3
    elif pet:
        why.append("pet_unnamed"); score += 2
    date = (m[1] if (m := TAKEN.search(text)) else None)
    if date:
        why.append("dated"); score += 1
    if "Taken the same day:" in text:
        why.append("part_of_a_day"); score += 1
    when = f"from {_month_year(date)}" if date else "(I don't know when it was taken)"
    others = PEOPLE.findall(desc)
    kinds = {o.lower() for o in others} - {"selfie"}
    just_a_man = kinds <= {"man"} and len(re.findall(r"\b(?:a|one) man\b", desc, re.I)) <= 1   # one man (a selfie or not)
    if people and just_a_man:   # most likely the owner himself: ask, don't assume
        ask = "Is that you, and what was happening that day?"
    elif people:
        ask = "Who is in it, and what was happening that day?"
    elif pet:
        ask = "Which of our pets is that?" if not re.search(r"\bcat|kitt|tabby", lab, re.I) else "Which of our cats is that?"
    else:
        ask = "Where was that, and what's the story behind it?"
    return {"id": f"photo:{mid}", "memory_id": mid, "period": None, "score": score, "why": why,
            "text": f"I have a photo {when} of {lab}. {ask}"}


def photo_questions(store) -> list[dict]:
    """Every uncaptioned photo as a question, best first (ties: older id first, so the order is stable)."""
    rows = store.db.execute("SELECT id, content FROM memories WHERE source LIKE 'photo:%' AND hidden IS NULL "
                            "ORDER BY id").fetchall()
    qs = [q for r in rows if (q := photo_question(r[0], r[1]))]
    return sorted(qs, key=lambda q: (-q["score"], q["memory_id"]))


def report(store, curiosity, learning) -> dict:
    """What Echo would bring up next and why (/api/initiative). Question texts describe photos only."""
    st = curiosity.state()
    asked = set(st.get("asked", []))
    photos = [q for q in photo_questions(store) if q["id"] not in asked]
    try:
        approved = learning.approved_questions()
    except Exception:  # noqa: BLE001
        approved = []
    return {"next_kind": curiosity.next_kind(), "approved_objectives": len(approved),
            "photos_without_caption": len(photos),
            "top_photo_questions": [{k: q[k] for k in ("id", "score", "why", "text")} for q in photos[:10]],
            "bank_questions_left": len([q for q in curiosity.questions if q["id"] not in asked | curiosity.answered]),
            "asked_so_far": len(asked), "rules": "the owner's voice only; at most every 30 min or when they say \"ask me "
            "something\"; approved objectives first, then a photo and a bank question in turn"}

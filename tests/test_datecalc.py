"""Date / clock sums worked out exactly (core/datecalc.py); the note reaches the model once and is never stored."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from core import datecalc
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore

T = dt.date(2026, 11, 1)   # a Sunday


@pytest.mark.parametrize("q,want", [
    ("If today is Wednesday, what day will it be in 10 days?", "Wednesday + 10 days = Saturday"),
    ("If today's Friday what day is it in a week?", "Friday + 7 days = Friday"),
    ("A train leaves at 2:45 PM and the trip takes 1 hour 50 minutes. What time does it arrive?",
     "2:45 PM + 1 hour and 50 minutes = 4:35 PM"),
    ("We leave at 11:30 pm and drive 2 hours, when do we arrive?", "11:30 PM + 2 hours = 1:30 AM the next day"),
    ("What day will it be in three days?", "in 3 days it will be Wednesday, November 4, 2026"),
    ("What's the date in 2 weeks?", "14 days from today is Sunday, November 15, 2026"),
    ("How many days until Christmas?", "Christmas is Friday, December 25, 2026, 54 days away"),
    ("How many days till March 14th?", "March 14 is Sunday, March 14, 2027, 133 days away"),   # already passed → next year
])
def test_answers(q, want):
    assert want in datecalc.answer(q, T)


@pytest.mark.parametrize("q", ["What's 15 percent of 80?", "Good morning!", "What time is it?",
                               "It takes 10 minutes to get there.", "What did we talk about on Thursday?",
                               "How many minutes are in three and a half hours?"])
def test_leaves_other_questions_alone(q):
    assert datecalc.answer(q, T) is None and datecalc.note(q, T) == ""


def test_cross_check_with_python_dates():
    for start in range(7):
        for n in (1, 6, 10, 23, 100):
            q = f"If today is {datecalc.DAYS[start]}, what day will it be in {n} days?"
            assert datecalc.answer(q, T).endswith(datecalc.DAYS[(start + n) % 7].title())


class Capture(EchoBackend):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def chat_stream(self, messages):
        self.sent.append(messages[-1]["content"])
        async for p in super().chat_stream(messages):
            yield p


def test_note_sent_once_and_not_stored(tmp_path):
    b, store = Capture(), MemoryStore(tmp_path / "m.sqlite")
    client = TestClient(create_app(PersonaConfig(), b, store))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "If today is Wednesday, what day will it be in 10 days?"})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    assert "[Date check, worked out exactly: Wednesday + 10 days = Saturday." in b.sent[-1]
    assert all("Date check" not in t["content"] for t in store.turns_window(as_sent=True) if t["role"] == "user")

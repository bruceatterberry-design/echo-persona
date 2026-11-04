"""Dream insights become lessons, never memories; capped so Echo stays fast."""
import json

import pytest

from core import trainer as training


@pytest.fixture
def t(tmp_path):
    lp = tmp_path / "lessons.json"
    lp.write_text(json.dumps({"lessons": [{"id": 1, "text": "An old lesson.", "active": True}]}))
    reloads = []
    tr = training.Trainer(tmp_path / "training.sqlite", lp, tmp_path / "req.md", None,
                          instructions=lambda: "", on_lessons=lambda: reloads.append(1))
    return tr, lp, reloads


def test_insight_becomes_lesson_and_reloads(t):
    tr, lp, reloads = t
    les = tr.add_insight("When a promise to a grandchild comes up, I may gently ask whether it happened.", "dream-insight:a", "owner")
    assert les["kind"] == "insight" and les["id"] == 2 and reloads
    assert "promise to a grandchild" in training.lessons_block(lp)


def test_cap_size_and_duplicates(t):
    tr, lp, _ = t
    for i in range(training.MAX_INSIGHTS):
        tr.add_insight(f"General insight number {i} for testing.", f"dream-insight:{i}", "owner")
    with pytest.raises(OverflowError):
        tr.add_insight("One insight too many for the cap.", "dream-insight:x", "owner")
    assert tr.drop_insight("dream-insight:0")
    tr.add_insight("Now there is room again for one more.", "dream-insight:x", "owner")
    with pytest.raises(ValueError):
        tr.add_insight("x" * (training.INSIGHT_CHARS + 1), "dream-insight:y", "owner")
    with pytest.raises(ValueError):
        tr.add_insight("Now there is room again for one more.", "dream-insight:x", "owner")
    assert len([x for x in tr.lessons(active=True) if x.get("kind") == "insight"]) == training.MAX_INSIGHTS



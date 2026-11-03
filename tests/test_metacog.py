"""CA 2.0 Phase 6: name check and correction check (core/metacog.py)."""
from core import metacog
from core.server import strip_hints

KNOWN = frozenset({"milo", "daisy", "loved_one", "owner", "peoria"})
CAT_PHOTO = "A photo in my \"Cats\" collection. What the photo shows: A tabby cat with dark stripes lies on a gray perch."


def test_name_check_only_when_photos_found_dont_name_them():
    pets = frozenset({"milo", "daisy"})
    n = metacog.name_note("Which of my cats is lying on the gray cat tree?", KNOWN, [CAT_PHOTO], pets)
    assert n.startswith("[Memory check:") and "Daisy or Milo" in n             # "my cats": do photos name any of them?
    assert metacog.name_note("Which of my cats is on the tree?", KNOWN, ["Alex says: Daisy on the tree."], pets) == ""
    n = metacog.name_note("Is that Milo on the gray cat tree?", KNOWN, [CAT_PHOTO])
    assert n.startswith("[Memory check:") and "Milo" in n and "don't guess" in n
    assert metacog.name_note("How many photos of Milo do I have?", KNOWN, [CAT_PHOTO, CAT_PHOTO]).startswith("[Memory check:")
    named = "A photo in my \"Cats\" collection. Alex says: Milo on the cat tree. What the photo shows: a tabby cat."
    assert metacog.name_note("How many photos of Milo do I have?", KNOWN, [named]) == ""   # a caption names him
    assert metacog.name_note("How many photos of Milo do I have?", KNOWN, []) == ""        # no photos found
    assert metacog.name_note("Tell me about Milo", KNOWN, [CAT_PHOTO]) == ""               # not which/who/how many


def test_correction_check():
    for t in ("Are you sure? I think it's 10.", "Actually, I think that one was taken in 2021.", "No, it was June.",
              "That's not right.", "You're wrong about that", "I think it was Tuesday"):
        assert metacog.correction_note(t).startswith("[Correction check:"), t
    for t in ("I think we should go to the lake", "Actually let's talk about music", "What do you think?"):
        assert metacog.correction_note(t) == "", t


def test_checks_never_stay_in_the_history():
    sent = "Are you sure?\n\n" + metacog.correction_note("Are you sure?") + "\n\n" + \
           metacog.name_note("How many photos of Milo do I have?", KNOWN, [CAT_PHOTO])
    assert strip_hints(sent) == "Are you sure?"


def test_recompute_and_exact_result_in_the_note():
    assert metacog.recompute("What is 15 percent of 80?") == "15% of 80 = 12"
    assert metacog.recompute("what's 7 times 8") == "7 × 8 = 56"
    assert metacog.recompute("12.5% of 40") == "12.5% of 40 = 5"
    assert metacog.recompute("10 divided by 0") is None and metacog.recompute("How are you?") is None
    n = metacog.correction_note("Are you sure? I think it's 10.", "What is 15 percent of 80?")
    assert "Worked out exactly: 15% of 80 = 12." in n
    assert "Worked out exactly" not in metacog.correction_note("Are you sure?", "When was the dam photo taken?")


def test_count_note_counts_in_code():
    photos = ["Alex says: A picture of Milo. What the photo shows: a tabby.", "What the photo shows: a calico.",
              "Alex says: Milo holding Sam with his paw.", "What the photo shows: a lake."]
    n = metacog.count_note("How many photos of Milo do I have?", KNOWN, photos)
    assert "counted exactly: 2 of my 4 photo memories mention Milo" in n
    assert metacog.count_note("How many photos of the lake?", KNOWN, photos) == ""     # not a known name
    assert metacog.count_note("How many photos of Milo?", KNOWN, []) == ""

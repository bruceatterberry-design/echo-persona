import pytest


@pytest.fixture(autouse=True)
def no_real_voice_profiles(tmp_path, monkeypatch):
    """Tests never load the real voice profiles in data/speakers (tests that need profiles pass their own)."""
    import core.server
    from voice.speaker import SpeakerID
    monkeypatch.setattr(core.server, "SpeakerID",
                        lambda model, _dir, *a: SpeakerID(model, tmp_path / "no-speakers", *a))

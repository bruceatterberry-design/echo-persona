import pytest


@pytest.fixture(autouse=True)
def no_real_voice_profiles(tmp_path, monkeypatch):
    """Tests never load the real voice profiles in data/speakers (tests that need profiles pass their own)."""
    import core.server
    from voice.speaker import SpeakerID
    monkeypatch.setattr(core.server, "SpeakerID",
                        lambda model, _dir, *a: SpeakerID(model, tmp_path / "no-speakers", *a))


@pytest.fixture(autouse=True)
def no_real_chrome_or_youtube(monkeypatch):
    """Tests never open Chrome, look things up on YouTube or download audio (a test once opened a real video).
    Tests that check these pass their own fakes on top."""
    from core import browser, music
    monkeypatch.setattr(browser, "youtube_top", lambda q, r=False: None)
    import subprocess
    import types
    monkeypatch.setattr(browser, "subprocess", types.SimpleNamespace(   # only the browser's copy, not Python's own
        run=lambda *a, **k: None, Popen=lambda *a, **k: None, DEVNULL=subprocess.DEVNULL,
        SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(music, "fetch_info", lambda vid: (_ for _ in ()).throw(RuntimeError("no YouTube in tests")))
    monkeypatch.setattr(music, "download_audio", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no YouTube in tests")))


@pytest.fixture(autouse=True)
def no_real_full_browser(monkeypatch):
    """Tests never start the invisible Chrome (core/jsbrowser.py); tests that check it fake it themselves."""
    from core import jsbrowser, web
    def blocked(url):
        raise web.FetchError("no full browser in tests")
    monkeypatch.setattr(jsbrowser, "render", blocked)


@pytest.fixture(autouse=True)
def fresh_scheduler(tmp_path, monkeypatch):
    """Each test gets its own conversation-first scheduler (core/sched.py is one per process): a conversation in one
    test must not keep another test's background work waiting. Memory is never "low" in tests; settings are defaults."""
    import core.sched as sched
    s = sched.Scheduler(settings=tmp_path / "no-scheduler.json", memory_probe=lambda: 50)
    monkeypatch.setattr(sched, "SCHED", s)
    for mod in ("core.server", "core.llm"):
        m = __import__(mod, fromlist=["x"])
        if hasattr(m, "SCHED"):
            monkeypatch.setattr(m, "SCHED", s)
    return s


@pytest.fixture(autouse=True)
def household_names():
    """Example names for the household (config/echo.yaml owner_name / loved_one_name): Alex and Sam."""
    from core import people
    people.configure("Alex", "Sam")
    yield
    people.configure("", "")

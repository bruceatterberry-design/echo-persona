import pytest
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from voice.stt import FakeSTT
from voice.wake import detect_wake
from tests.test_voice import synthetic_wav


@pytest.mark.parametrize("text,command", [
    ("Hey Echo, what's the weather like?", "what's the weather like?"),
    ("hey eko what time is it", "what time is it"),
    ("Hey, Ecco. Remember that I like fishing.", "Remember that I like fishing."),
    ("Okay Echo tell me a story", "tell me a story"),
    ("Um, hey Echo, are you there?", "are you there?"),
    ("Hey Echo.", ""),
    ("Hi echo", ""),
    ("A echo, are you awake?", "are you awake?"),
    ("Hey Nako.", ""),                      # how a bare "Hey Echo" is usually heard
    ("Hey Neko, what time is it?", "what time is it?"),
    ("Hecho.", ""),
    ("Fay Echo.", ""),
])
def test_wake_detected(text, command):
    assert detect_wake(text) == (True, command)


@pytest.mark.parametrize("text", [
    "I was talking to my wife about dinner.",
    "There's an echo in this room.",
    "Thanks, echo, I'll be home soon.",   # name without a greeting: not a wake
    "There's an echo in this room.",
    "Hey Steve, how are you?",
    "The canyon has a great echo.",
    "Echo chambers are a problem online.",
    "Dicho y hecho.",                     # fused form only counts at the very start
    "My cat Neko is asleep.",
    "She's such a echo.",
    "What a echo pie he is.",
    "",
])
def test_not_woken(text):
    assert detect_wake(text) == (False, "")


def client(tmp_path, said):
    return TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT(said)))


def test_wake_endpoint_discards_other_speech(tmp_path):
    r = client(tmp_path, "my bank PIN is 1234").post("/api/stt?mode=wake", content=synthetic_wav()).json()
    assert r == {"wake": False}          # transcript must not be returned


def test_wake_endpoint_returns_command(tmp_path):
    r = client(tmp_path, "Hey Echo, who am I?").post("/api/stt?mode=wake", content=synthetic_wav()).json()
    assert r == {"wake": True, "command": "who am I?"}


def test_direct_mode_unchanged(tmp_path):
    assert client(tmp_path, "hello").post("/api/stt", content=synthetic_wav()).json() == {"text": "hello"}


from voice.wake import detect_end, is_noise_transcript


@pytest.mark.parametrize("text", ["That's all, thanks.", "Okay goodbye Echo", "Stop listening",
                                  "Thanks Echo, that is all", "Never mind.", "Bye Echo", "We're done for now."])
def test_end_detected(text):
    assert detect_end(text)


@pytest.mark.parametrize("text", ["What's all this about?", "Tell me about my goodbye party",
                                  "Is that all you know about fishing?" , "How do I stop the car from listening to me"])
def test_end_not_detected(text):
    # "that all"/"stop ... listening" in a normal sentence must not end the conversation
    assert not detect_end(text)


def test_noise_transcripts():
    assert is_noise_transcript("Thanks for watching!") and is_noise_transcript(" you ")
    assert not is_noise_transcript("Thank you, that helps.")


def test_conversation_mode_endpoint(tmp_path):
    assert client(tmp_path, "And what about tomorrow?").post("/api/stt?mode=conversation", content=synthetic_wav()).json() \
        == {"text": "And what about tomorrow?", "end": False}
    assert client(tmp_path, "Okay, that's all.").post("/api/stt?mode=conversation", content=synthetic_wav()).json()["end"] is True
    assert client(tmp_path, "Thanks for watching.").post("/api/stt?mode=conversation", content=synthetic_wav()).json() == {"text": ""}

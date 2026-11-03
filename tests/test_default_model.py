"""1.0.0: new installs start with Qwen 3.5 9B, thinking off, Qwen's sampling settings; anything set by hand wins."""
from core.config import LLMConfig, PersonaConfig


def test_default_is_qwen_without_thinking():
    c = PersonaConfig().llm
    assert c.model == "qwen3.5:9b-q4_K_M" and c.think is False and c.temperature == 0.4
    assert c.options == {"top_p": 0.8, "top_k": 20, "min_p": 0, "presence_penalty": 1.5}


def test_own_choices_win():
    assert LLMConfig(model="qwen3.5:9b-q4_K_M", think=True).think is True
    assert LLMConfig(model="qwen3.5:9b-q4_K_M", options={"top_p": 0.9}).options == {"top_p": 0.9}


def test_llama_is_left_alone():
    c = LLMConfig(model="llama3.1:8b", temperature=0.7)
    assert c.think is None and c.options == {} and c.temperature == 0.7

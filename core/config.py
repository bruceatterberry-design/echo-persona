"""Configuration loading. Real configs live in config/*.yaml (gitignored)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent


class LLMConfig(BaseModel):
    backend: str = "ollama"  # "ollama" | "echo"
    host: str = "http://127.0.0.1:11434"
    # 1.0.0: Qwen 3.5 9B replaced Llama 3.1 8B as the default (fewer made-up details, follows the conversation better;
    # measured in docs/BENCHMARKS.md). To go back: model: llama3.1:8b and temperature: 0.7 in config/echo.yaml.
    model: str = "qwen3.5:9b-q4_K_M"
    temperature: float = Field(0.4, ge=0.0, le=2.0)
    timeout_s: float = 120.0
    keep_alive: str = "24h"  # keep the model in memory so a reply after a quiet spell isn't slow
    think: bool | None = None   # thinking models (qwen3.5): True/False; None = don't say (other models)
    options: dict[str, float | int] = Field(default_factory=dict)   # extra sampling settings: top_p, top_k, min_p, …

    @model_validator(mode="after")
    def _qwen_defaults(self):
        """Qwen 3.5 thinks for minutes before its first word unless told not to, and wants its own sampling settings.
        Applied only when nothing was set, so any choice in config/echo.yaml wins."""
        if str(self.model).startswith("qwen3.5"):
            if self.think is None:
                self.think = False
            if not self.options:
                self.options = {"top_p": 0.8, "top_k": 20, "min_p": 0, "presence_penalty": 1.5}
        return self


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"  # never bind publicly by default
    port: int = Field(8765, ge=1024, le=65535)


class VoiceConfig(BaseModel):
    stt_model: str = "mlx-community/whisper-large-v3-turbo"
    language: str | None = "en"
    tts_url: str = "http://127.0.0.1:8766"  # the owner's cloned voice service


class PersonaConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    voice: VoiceConfig = VoiceConfig()
    server: ServerConfig = ServerConfig()
    personality_file: str = "config/personality.yaml"
    owner_name: str = ""       # the owner's first name: "This is <name>" teaches Echo their voice; "Is that you, <name>?"
    loved_one_name: str = ""   # the person Echo mostly talks with; their name is used only now and then (blank = off)
    log_dir: str = "logs"
    memory_db: str = "data/memory.sqlite"  # relative to project root unless absolute
    backup_dir: str = "data/backups"
    dataset_root: str = "/Volumes/External storage/EchoPersona-Data"  # external SSD
    personality_dir: str = "data/personality"   # draft + the owner-approved profile
    # who's talking (voice/speaker.py): voice profiles + model; threshold/margin from scripts/bench_speaker.py
    speakers_dir: str = "data/speakers"
    speaker_model: str = "models/speaker/wespeaker_en_voxceleb_resnet34.onnx"
    speaker_threshold: float = 0.72
    speaker_margin: float = 0.1
    # Echo asks the owner about their life (memory/curiosity.py): only when the voice is the owner's
    ask_enabled: bool = True
    ask_every_minutes: float = 30
    ask_min_turns: int = 4

    def resolve(self, p: str) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else ROOT / path


def load_config(path: str | Path | None = None) -> PersonaConfig:
    path = Path(path) if path else ROOT / "config" / "echo.yaml"
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    cfg = PersonaConfig.model_validate(data)
    if (m := os.environ.get("ECHO_LLM_MODEL")):   # benchmarks only: try another model without editing echo.yaml
        cfg.llm.model = m
    return cfg


def load_personality(cfg: PersonaConfig) -> str:
    p = ROOT / cfg.personality_file
    if not p.exists():
        p = ROOT / "config" / "personality.example.yaml"
    data = yaml.safe_load(p.read_text()) or {}
    return str(data.get("system_prompt", "")).strip()

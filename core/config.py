"""Configuration loading. Real configs live in config/*.yaml (gitignored)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class LLMConfig(BaseModel):
    backend: str = "ollama"  # "ollama" | "echo"
    host: str = "http://127.0.0.1:11434"
    model: str = "llama3.1:8b"
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    timeout_s: float = 120.0
    keep_alive: str = "24h"  # keep the model in memory so a reply after a quiet spell isn't slow


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
    loved_one_name: str = ""   # the person Echo mostly talks with; their name is used only now and then (blank = off)
    log_dir: str = "logs"
    memory_db: str = "data/memory.sqlite"  # relative to project root unless absolute
    backup_dir: str = "data/backups"
    dataset_root: str = "/Volumes/External storage/EchoPersona-Data"  # external SSD
    personality_dir: str = "data/personality"   # draft + the owner-approved profile

    def resolve(self, p: str) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else ROOT / path


def load_config(path: str | Path | None = None) -> PersonaConfig:
    path = Path(path) if path else ROOT / "config" / "echo.yaml"
    if not path.exists():
        return PersonaConfig()
    data = yaml.safe_load(path.read_text()) or {}
    return PersonaConfig.model_validate(data)


def load_personality(cfg: PersonaConfig) -> str:
    p = ROOT / cfg.personality_file
    if not p.exists():
        p = ROOT / "config" / "personality.example.yaml"
    data = yaml.safe_load(p.read_text()) or {}
    return str(data.get("system_prompt", "")).strip()

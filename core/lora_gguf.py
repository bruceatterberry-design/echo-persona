"""Bring an MLX-LM LoRA adapter (Qwen 3.5, MLP blocks only) into Ollama (daydream training step 6, 2026-11-04).
merge() is the route that works with Ollama 0.40; convert() writes a GGUF adapter file, which Ollama now refuses but
plain llama.cpp still accepts.

Why not mlx_lm fuse: a full-precision copy is ~17 GB plus another copy inside Ollama, more than this Mac's free disk;
merge() edits only the trained tensors of Echo's existing GGUF (undo = use the old model name).
Why MLP only: Qwen 3.5's linear-attention tensors are packed differently in GGUF, so adapters on them don't map
one-to-one; gate/up/down_proj map exactly onto blk.N.ffn_gate/ffn_up/ffn_down (checked against the installed GGUF).

Math: MLX computes y = x·W + scale·(x·A)·B with A (in, r), B (r, out). GGUF/llama.cpp stores lora_a as (r, in) and
lora_b as (out, r) in numpy order and applies (alpha / r)·B·A, so lora_a = Aᵀ, lora_b = Bᵀ, alpha = scale·r.
Runs in the training environment (.venv-train: mlx, gguf), not in Echo's own.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROJ = {"gate_proj": "ffn_gate", "up_proj": "ffn_up", "down_proj": "ffn_down"}
KEY = re.compile(r"^language_model\.model\.layers\.(\d+)\.mlp\.(gate_proj|up_proj|down_proj)\.lora_([ab])$")


def gguf_name(mlx_key: str) -> str:
    """'language_model.model.layers.24.mlp.down_proj.lora_a' → 'blk.24.ffn_down.weight.lora_a'."""
    m = KEY.match(mlx_key)
    if not m:
        raise ValueError(f"not an MLP LoRA tensor this converter handles: {mlx_key}")
    return f"blk.{m[1]}.{PROJ[m[2]]}.weight.lora_{m[3]}"


def convert(adapter_dir: Path, out_path: Path, arch: str = "qwen35") -> dict:
    import gguf
    import mlx.core as mx
    import numpy as np

    cfg = json.loads((adapter_dir / "adapter_config.json").read_text())
    lp = cfg["lora_parameters"]
    rank, scale = int(lp["rank"]), float(lp["scale"])
    weights = mx.load(str(adapter_dir / "adapters.safetensors"))
    w = gguf.GGUFWriter(str(out_path), arch)
    w.add_string("general.type", "adapter")
    w.add_string("adapter.type", "lora")
    w.add_float32("adapter.lora.alpha", scale * rank)
    n = 0
    for k in sorted(weights):
        a = np.array(weights[k].astype(mx.float32)).T.copy()     # MLX (in, r)/(r, out) → (r, in)/(out, r)
        w.add_tensor(gguf_name(k), a.astype(np.float32))
        n += 1
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()
    return {"tensors": n, "rank": rank, "alpha": scale * rank, "path": str(out_path)}


def merge(base_gguf: Path, adapter_dir: Path, out_path: Path) -> dict:
    """Ollama 0.40 refuses ADAPTER ("LoRA adapters are no longer supported", tested 11-04), so bake the adapter into
    a copy of Echo's own GGUF: every tensor is copied byte for byte except the trained MLP blocks, which are
    dequantized, given W += (alpha/r)·Bᵀ·Aᵀ, and stored as Q8_0 (gguf-py can't write Q4_K; Q8_0 is closer to the
    trained weights anyway). 8 trained layers ≈ +0.6 GB over the 5.6 GB original."""
    import gguf
    import gguf.quants as q
    import mlx.core as mx
    import numpy as np

    cfg = json.loads((adapter_dir / "adapter_config.json").read_text())
    rank, scale = int(cfg["lora_parameters"]["rank"]), float(cfg["lora_parameters"]["scale"])
    ad = {gguf_name(k): np.array(v.astype(mx.float32)) for k, v in mx.load(str(adapter_dir / "adapters.safetensors")).items()}
    targets = {k[: -len(".lora_a")] for k in ad if k.endswith(".lora_a")}
    r = gguf.GGUFReader(str(base_gguf))
    arch = r.fields[gguf.Keys.General.ARCHITECTURE].contents()
    w = gguf.GGUFWriter(str(out_path), arch, endianess=r.endianess)
    for f in r.fields.values():
        if f.name == gguf.Keys.General.ARCHITECTURE or f.name.startswith("GGUF."):
            continue
        sub = f.types[-1] if f.types[0] == gguf.GGUFValueType.ARRAY else None
        w.add_key_value(f.name, f.contents(), f.types[0], sub_type=sub)
    new = {}
    for t in r.tensors:
        if t.name in targets:
            base = q.dequantize(t.data, t.tensor_type).astype(np.float32)          # (out, in)
            a, b = ad[t.name + ".lora_a"], ad[t.name + ".lora_b"]                   # MLX A (in, r), B (r, out)
            delta = scale * (a @ b).T                                               # (out, in)
            if delta.shape != base.shape:
                raise ValueError(f"{t.name}: adapter {delta.shape} ≠ model {base.shape}")
            new[t.name] = q.quantize(base + delta, gguf.GGMLQuantizationType.Q8_0)
            d = new[t.name]
            w.add_tensor_info(t.name, d.shape, d.dtype, d.nbytes, gguf.GGMLQuantizationType.Q8_0)
        else:
            w.add_tensor_info(t.name, t.data.shape, t.data.dtype, t.data.nbytes, t.tensor_type)
    missing = targets - set(new)
    if missing:
        raise ValueError(f"adapter tensors not found in the model: {sorted(missing)[:3]}")
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_ti_data_to_file()
    for t in r.tensors:
        w.write_tensor_data(new.get(t.name, t.data), tensor_endianess=r.endianess)
    w.close()
    return {"changed": len(new), "rank": rank, "scale": scale, "path": str(out_path)}


if __name__ == "__main__":
    import sys
    if sys.argv[1] == "merge":
        print(merge(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
    else:
        print(convert(Path(sys.argv[1]), Path(sys.argv[2])))

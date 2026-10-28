"""End-to-end smoke test against the real local model (not run in CI)."""
import asyncio, time
from core.config import load_config, load_personality
from core.llm import make_backend

async def main():
    cfg = load_config(); b = make_backend(cfg.llm)
    print("health:", await b.health())
    sp = load_personality(cfg)
    for q in ["Hi Echo, who are you?", "What's my favorite food?"]:
        t = time.perf_counter()
        r = await b.chat([{"role": "system", "content": sp}, {"role": "user", "content": q}])
        print(f"\n[{time.perf_counter()-t:.1f}s] Q: {q}\n  -> {r.model_dump()}")
asyncio.run(main())

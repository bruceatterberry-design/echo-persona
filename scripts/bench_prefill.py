"""Prompt-reading speed of the local model: a fixed ~1,500-token prompt, cache defeated with a random prefix."""
import random, httpx, statistics
words = ("the quiet morning light falls across the porch while the coffee cools and the birds talk in the old oak " * 80).split()
rates = []
for i in range(3):
    prompt = f"[{random.random()}] " + " ".join(words[:1100]) + "\nSay OK."
    r = httpx.post("http://127.0.0.1:11434/api/generate", json={"model": "llama3.1:8b", "prompt": prompt, "stream": False,
                   "options": {"num_predict": 2}, "keep_alive": "24h"}, timeout=300).json()
    rates.append(r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9))
    print(f"  {r['prompt_eval_count']} tokens read in {r['prompt_eval_duration']/1e9:.2f}s = {rates[-1]:.0f} tokens/s")
print(f"median: {statistics.median(rates):.0f} tokens/s")

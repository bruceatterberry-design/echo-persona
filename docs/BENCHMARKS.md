# Benchmarks (measured on Mac mini M4, 16 GB)

## Voice cloning, 2026-10-27 (mlx-audio 0.6.2, zero-shot from one 6.2 s reference take)
Test sentence (not in the recordings): "Hi, I'm Echo, the owner's digital self. It's good to talk with you. Let me
think about that for a second, because it's a really good question."

| Model | Download | License | Audio | Generation time | Peak memory | Whisper check |
|---|---|---|---|---|---|---|
| Chatterbox multilingual v3 (+ S3TokenizerV2 495 MB) | 2.7 GB | MIT (tokenizer card unlisted; CosyVoice upstream Apache-2.0) | 7.06 s | 7.49 s (≈ real time) | 3.66 GB | word-perfect |
| Qwen3-TTS 12Hz 0.6B Base bf16 | 2.5 GB | Apache-2.0 | 7.76 s | 19.74 s (≈ 21 tokens/s after a slow start) | 6.75 GB | word-perfect |

Times include model load from a cold process. A persistent service would avoid the reload.

## Speech recognition (mlx-whisper large-v3-turbo)
- ≈ 0.85–0.94 s for a 5 s utterance (warm); cold load ≈ 15 s.

## LLM (Ollama llama3.1:8b)
- First reply 5.1 s (includes load), then ≈ 1.8–3 s per short reply.

## Voice reference comparison, 2026-10-27 (Qwen3-TTS, blind, the owner judged)
Sentence: "Good morning, sweetheart. I was just thinking about you, and I wanted to say I hope you have a really good day today."
| Reference | Length | SNR | Time to make (warm) | Whisper | the owner's pick |
|---|---|---|---|---|---|
| storytelling (studio USB mic) | 6.2 s | 40 dB | 6.5 s | word-perfect | **chosen** |
| phone-conversational | 4.7 s | 51 dB | 12.9 s | word-perfect | |
| phone-pair (2 phone clips joined) | 9.7 s | ~51 dB | 11.7 s | word-perfect | |
Cleaner audio (higher SNR) did not mean a better likeness to the owner's ear.

## Speech recognition: Whisper vs Parakeet, 2026-10-27 (the owner's own recordings)
110 read-aloud takes (studio + phone) with known text, 1,406 words.
| Recognizer | Word error rate | Median time per clip | Wake phrases (the owner's cloned voice) | Wake phrases (4 macOS voices) |
|---|---|---|---|---|
| Whisper large-v3-turbo (via Echo Core) | 1.71% (24 words) | 0.94 s | 8/8 | 24/24 |
| **Parakeet TDT 0.6B v2 (chosen)** | **1.71% (24 words)** | **0.13 s** | **8/8** | 23/24 ("Hey Echo" → "Hey sleeve" in the old robotic "Fred" voice) |
Through Echo after the switch: 0.13 s for a 2.5 s question (was 0.9 s); spoken conversation test passed, 0 fallbacks.
Tool: `tts/compare_stt.py`.

## Reply speed after prompt-reuse fix, 2026-10-27
First word 0.15–0.33 s (median 0.21 s) vs 3.4–4.2 s before; prompt reading on the M4 ≈ 215 tokens/s (KV cache q8/f16 and
flash attention make no difference). Voice first sound 0.38–0.45 s with 0.4 s stream pieces (was 0.7 s), gapless.
Tools: `scripts/bench_reply.py`, `scripts/bench_prefill.py`.

## Optional Claude helper vs the local model, 2026-10-27 (made-up transcript, no personal data)
One personality-analysis job on a 330-word made-up transcript:
| Model | Time | Traits | With exact quotes (kept) |
|---|---|---|---|
| llama3.1:8b on the M4 | 30.0 s | 8 | 8 |
| Claude Sonnet via the owner's Claude Code account | 6.8 s | 8 | 8 |
Connection check from the running Echo Core service: 3.4 s. While Claude does a job, the Mac's memory and GPU stay free
for the loved one's conversations.

## Memory Phase 1: originals catalogue + episodes, 2026-10-31 (live data, M4)
| Measure | Result |
|---|---|
| First catalogue of raw/ (747 files, 1.07 GB, sha256) | ~2 s wall, peak RSS 118 MB |
| Re-verify all 747 checksums | 0.7 s; 747 OK |
| Catalogue vs checksums recorded at recording time | 348/348 match |
| Conversation archive (3 days, 624 turns) | JSONL 130 KB → xz 25 KB (19 %); read back identical to the DB |
| Episodes | 8 (546 turns); turn → episode lookup 0.002 ms |
| Old notes linked to turns | 38/38; each note's date matches its turns |

## Memory Phase 2: fact extraction, 2026-10-31 (llama3.1:8b, all 69 live memories, hand-checked by Claude)
| Version | Facts kept | Wrong | Precision | Time |
|---|---|---|---|---|
| 1: quote must be in the memory, predicate on the list | 110 | 27 | 75 % | 729 s |
| 2: + subject named, no "can" from plans, object in the memory | 97 | 19 | 80 % | 679 s |
| 3: + named objects in the quote, feeling words for likes | 85 | 10 | 88 % | 551 s |
| 3 + plans anywhere in the sentence (replayed on saved answers) | 81 | 6 | **92.6 %** | — |
Borderline facts counted as right (7, e.g. "the owner said the American national anthem"); counting them wrong: 84 %.
Recall (facts missed) not measured. about(name) over HTTP: see STATUS.

## Memory Phase 3: retrieval, old 2 extras vs engine v2, 2026-10-31 (98 questions, live memories, now = Oct 31 01:00)
| | old (as used) | new top 2 | new top 5 |
|---|---|---|---|
| hit (88 answerable) | 75 (85 %) | 84 (95 %) | 84 (95 %) |
| MRR | 0.835 | 0.938 | 0.938 |
| precision of what's returned | 73 % | 79 % | 67 % |
| quiet on 10 "I don't know" questions | 2 | 7 | 7 |
| time questions (7) | 2 | 7 | 7 |
| odd / even half, hit@2 | 77 % / 93 % | 91 % / 100 % | |
Engine 1.6 ms p50, 3.4 ms p95, 5.1 ms max; query vector 14 ms p50. Rules were tuned on these questions: treat as an
upper bound until the owner adds new ones. `PYTHONPATH=. uv run python scripts/bench_memory.py [--show]`

## Echo's LLM: llama3.1:8b → qwen3.5:9b-q4_K_M, 2026-10-31 (chart: docs/llm-comparison.html)
| | llama3.1:8b | qwen3.5:9b MLX (removed) | qwen3.5:9b-q4_K_M before fix | qwen3.5:9b-q4_K_M now |
|---|---|---|---|---|
| memory (Ollama) | 5.4 GB | 7.2 → 15.6 GB (prefix cache) | 4.9 GB | 4.9 GB |
| first word, ongoing conversation (median) | 1.7 s | ~0.3 s | 5.0 s | 1.6 s (reset turn 5.9 s) |
| made-up details, 16 questions (strict) | 3, 3 | 2–5 | 1, 3 | 1, 3 |
| facts extracted (hand-checked) | 81, 92.6 % | 152, 89.5 % | — | — |
Thinking on: 118–223 s to first word. `scripts/bench_invention.py [--conversation --questions file]`.

## Memory Phase 6: scale, 2026-10-31 (Mac mini M4 16 GB, Echo running alongside)
**Vector compression on real data** (scripts/bench_compression.py; 638 memories + messages, 98 questions):
768-dim float32 → int8 keeps 98 % of the same top-10 (4× smaller); 1-bit 74 %; cutting to 512/256 dims 86 %/76 %.
Decision: keep 768 dims; int8 on disk; no truncation.

**Vector search at 1M / 2M** (scripts/bench_vectors.py; synthetic memories = Echo's real vectors + noise; 200 queries;
exact answers from float32):
| Method | N | build | in RAM | recall@10 | p50 per search |
|---|---|---|---|---|---|
| brute force int8 (numpy) | 1M | 3 s | 768 MB | 97.6 % | 238 ms |
| 1-bit prefilter + int8 re-check | 1M / 2M | 3 / 6 s | 768 / 1,536 MB | 94.8 / 92.4 % | 91 / 180 ms |
| usearch HNSW i8 (ef 64 / 128) | 1M | 49 s | 917 MB | 58.6 / 69.9 % | 0.19 / 0.32 ms |
| FAISS IVF4096 + SQ8 (nprobe 32) | 1M / 2M | 18 / 26 s | 789 / 1,565 MB | 98.0 / 98.2 % | 1.6 / 3.0 ms |
| **FAISS IVF4096 + PQ96, nprobe 64, re-check top 1,000 from int8 on disk** | 2M | 54 s | **221 MB** (+1.5 GB on disk) | **97.2 %** | **1.7 ms** |
Re-check reads were warm (just written); cold SSD reads not measured. Build peaks 2.6–4.2 GB (nightly job).

**Echo's own memory code at 1M memories** (scripts/bench_scale.py; 1,001,250 memories, 500k turns, 50k entities,
2M facts; synthetic text; DB 1.1 GB without vectors):
| Step | before fixes | after |
|---|---|---|
| question naming a very frequent person + topic | > 120 s | 19.5 ms |
| question naming a rare person | 669 ms | 0.1 ms |
| facts about the most frequent person | 1,912 ms | 6.9 ms |
| "everything about" the most frequent person | 20,863 ms | 74 ms |
| context pack "changed?" check (every message) | ~175 ms | < 0.1 ms |
| context pack build (only when memories change) | 9,820 ms | 369 ms |
| keyword search / time questions / turn→episode | 7 / 1.7 / 0.4 ms | same |
| nightly: guard / priorities | 1.2 s / 8.6 s | 1.4 s / 5.7 s |
| peak RAM of the process | 2,854 MB | 81 MB |
Real 98 questions after the changes: hit@5 97 % (unchanged), hit@2 94 % (one question now answered first by a newer
memory made after the questions were written).

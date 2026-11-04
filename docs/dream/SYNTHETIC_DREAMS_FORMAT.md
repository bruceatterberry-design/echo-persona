# Synthetic Dreams — ingestion format v1

A Synthetic Dreams file is **practice material for the Dream Engine on the tower**: worked examples of good
(and deliberately flawed) dreams, in exactly the shape Echo's real dreams use. The tower can use them to:
- tune its dream prompts (few-shot examples);
- calibrate the six evaluation scores;
- train its own checker to catch invented memories and bad quotes (from the "flawed" records).

**Rules that never bend:**
1. Synthetic dreams are **never memories**. They are not sent to the Mac, never enter Echo's memory, and can't
   support a real proposal. The Mac's dream reader rejects any proposal citing a `synthetic:` ref.
2. They use **made-up people, pets and places only**. No real names from Echo's life, and nothing about students,
   IEPs or classrooms. The Mac checker blocks these.
3. **Don't give ChatGPT Echo's real memories.** It only needs this document. It describes Echo's data structure; it
   doesn't contain Echo's data.

Check every file on the Mac before taking it to the tower:

```
cd ~/EchoPersona/dream_bridge && .venv/bin/python -m bridge check-synthetic ~/Downloads/synthetic_dreams.json
```

It prints `OK: ready to take to the tower.` or a list of exact problems, which you can paste back to ChatGPT.

## File

One UTF-8 JSON file (not JSON Lines), up to 5000 records:

```json
{
  "format": "echo-synthetic-dreams",
  "version": 1,
  "generator": "ChatGPT (model name), 2026-11-03",
  "created": "2026-11-03T22:00:00Z",
  "count": 500,
  "records": [ ...500 records... ]
}
```

`count` must equal the number of records.

## Record

| Field | Type | Rules |
|---|---|---|
| `synthetic_id` | string | `syn-0001` … `syn-9999`, unique |
| `synthetic` | `true` | always true |
| `theme` | string | 3–200 chars; varied (at least half the records should have different themes) |
| `dream_type` | string | `association` · `reflection` · `curiosity` · `goal` · `personality` · `discovery` · `contradiction` |
| `seed_memories` | list, 1–10 | the made-up memories this dream is "about", in Echo's memory shape (below) |
| `seed_entities` | list, 0–15 | made-up people / places / pets / things in those memories |
| `narrative` | string | 40–4000 chars; **must start with** `[Speculation: this is a synthetic dream, not a memory.]` |
| `proposals` | list, 1–8 | in Echo's real proposal shape (below) |
| `quality` | object | `{"label": "good", "flaw": null}` or `{"label": "flawed", "flaw": "<one of the flaws>"}` |

### seed_memories[] (same fields as Echo's real `memories` export)

| Field | Rules |
|---|---|
| `ref` | `synthetic:<synthetic_id>:m1`, `…:m2`, … |
| `kind` | `memory` (a life event or photo description) · `summary` (a conversation summary, e.g. "On Sunday we talked about…") · `fact` (a stated fact) · `knowledge` (something learned) |
| `content` | 10–2000 chars, first-person or third-person like Echo's real memories |
| `year` | 1900–2100 or null |
| `when_text` | e.g. "spring 1988", or null |
| `people` | comma-separated made-up names, or null |
| `place` | or null |
| `told_by` | always `"synthetic"` |

### seed_entities[]
`ref` (`synthetic:<synthetic_id>:e1`, …), `type` (`person` · `place` · `pet` · `thing` · `org` · `event` · `concept`),
`name`, `note` (optional).

### proposals[] (same as a real dream_result proposal, BRIDGE_PROTOCOL.md section 7)

| Field | Rules |
|---|---|
| `proposal_id` | `<synthetic_id>-p1`, `-p2`, … |
| `kind` | `association` · `question` · `goal` · `personality` · `contradiction` · `outdated` · `discovery` |
| `proposed_change` | 5–1000 chars, plain words |
| `reason` | 5–1000 chars |
| `supporting_refs` | 1–10 refs, **only** this record's own `synthetic:` seeds |
| `evidence` | 1–10 × `{"ref", "quote"}`; each quote **word for word** from that seed's text (except in `bad_quote` flawed records) |
| `confidence` | 0–1 |
| `consequences` | what would change if Echo accepted it |
| `scores` | all six, each 0–1: `identity`, `evidence`, `novelty`, `consistency`, `confidence`, `usefulness` |
| `goal` | only for kind `goal` (and required then): `{"horizon": "short\|medium\|long", "priority": 1-5, "dependencies": [], "review_date": "YYYY-MM-DD"}` |

### quality.flaw (for deliberately flawed records, at most 30% of the file; about 20% is ideal)
- `invented_memory`: a proposal states something no seed memory says.
- `speculation_as_fact`: a guess is stated as certain.
- `bad_quote`: an evidence quote isn't really in the seed.
- `irrelevant`: a link with no real connection.
- `repetitive`: a near-copy of an earlier record's idea.

Flawed records should get honest low scores (for example, a low `evidence` score for `invented_memory`).

## Checks both sides agree on (Mac `bridge check-synthetic` = tower `import-synthetic`, 2026-11-03)
1. Ratio rules (at most 30% flawed; at least half the themes different) are **errors from 10 records up**, warnings below 10.
2. Quote text: a seed memory's quote must be found word for word in its `content`, `when_text`, `people` and `place`, the
   non-empty ones joined by single spaces in that order. A seed entity's text is `name` + `note`. The same rule applies to
   real batches (§7).
3. Unknown fields are errors everywhere; `seed_entities[].note` may be missing; `goal` appears only on kind `goal`.
4. Blocked anywhere in a record (any case, whole word): student(s), pupil(s), classroom(s), IEP(s), lesson plan(s), FERPA,
   teacher tool(s); and the owner's and loved one's names. The Mac additionally blocks every real person and pet
   name in Echo's memory. That private list never leaves the Mac, and the Mac checks every file before it goes to the tower.

## Example
`synthetic_dreams_example.json` (next to this file) holds two complete records: one good, one flawed
(`invented_memory`). It passes the checker.

## Prompt to give ChatGPT (copy it along with this whole document and the example file)

```
Create a Synthetic Dreams file exactly as described in the attached SYNTHETIC_DREAMS_FORMAT.md,
following synthetic_dreams_example.json. 500 records, syn-0001 to syn-0500, one JSON file.
Use only made-up people, pets and places, and vary the themes widely: family, work, hobbies, places,
seasons, music, pets, learning, everyday life, aging, gratitude. About 20% of records should be
deliberately flawed (quality.label "flawed"), spread across the five flaw types, with honest low scores.
Every narrative starts with "[Speculation: this is a synthetic dream, not a memory.]". Every evidence quote is
copied word for word from its seed memory (except in bad_quote records). Output valid JSON only.
```

500 records is about 1–1.5 MB, so ask ChatGPT for a downloadable file (it may need to build it in batches of 50–100).

## On the tower
Taken to the tower by USB. The tower session builds the importer, validates again against this document, and stores
the records **apart from** real dreams and batches. It never sends them to the Mac as dreams or proposals.

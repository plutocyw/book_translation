# Traditional Chinese book translation pipeline

This repository turns a text-based or scanned PDF into resumable, paragraph-aware translation chunks, maintains a persistent terminology register, routes work to different model tiers, reviews only what needs review, and assembles the approved Traditional Chinese text.

The source PDF and generated book contents remain untracked by Git.

## Model routing

The defaults in `project.json` deliberately do not use the most expensive model everywhere:

| Role | Default | Why |
|---|---|---|
| Terminology discovery | `gpt-5.6-luna` | High-volume extraction with human approval before use |
| Draft translation | `gpt-5.6-terra` | Quality/cost balance for the prose itself |
| Bilingual review | `gpt-5.6-terra` | Strong source-target comparison without paying top tier for every chunk |
| Difficult adjudication | `gpt-5.6-sol` | Used only when review returns `escalate` |

Model IDs, reasoning effort, maximum output, and pricing assumptions are configuration rather than code. Change them in `project.json` if your account has different model access or after an evaluation pilot.

## Setup

Use Python 3.9 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
book-translate doctor
```

Model-backed commands require an API key in the environment:

```bash
export OPENAI_API_KEY="..."
```

Local inspection, OCR, extraction, chunking, estimation, assembly, and deterministic QA do not send content to a model.

### Codex-driven translation (no API key)

When Codex is the translation engine, the repository does not need an API key. Prepare self-contained work packets after extraction and chunking:

```bash
book-translate prepare --start 1 --limit 2
```

Codex reads `build/packets/*.translation.md`, writes the translations to `output/chunks/*.zh-Hant.md`, and can then review them directly. The `translate` and model-backed `review` commands remain available only for unattended API-based operation.

## First run

1. Place the PDF at `input/book.pdf` or change `source_pdf` in `project.json`.
   Use `source_page_start` and `source_page_end` to restrict extraction to the narrative body when the PDF contains front or back matter.
2. Fill in `context/project_brief.md` and `context/style_guide.md`.
3. Inspect and extract:

```bash
book-translate inspect
book-translate extract
book-translate chunk
book-translate estimate
```

If inspection reports that most pages have little text, OCR first:

```bash
book-translate ocr
```

Then point `source_pdf` at `build/ocr/book.ocr.pdf` and repeat inspection/extraction.

4. Run the terminology pass, review the CSVs, and change accepted entries from `provisional` to `approved`:

```bash
book-translate terms
```

5. Translate and review a small pilot before the whole book:

```bash
book-translate translate --start 1 --limit 2
book-translate review --start 1 --limit 2 --escalate
```

Review these files:

- `output/chunks/*.zh-Hant.md`
- `output/reviews/*.review.json`
- `context/glossary.csv`
- `context/characters.csv`

Adjust the style guide, glossary, and model routing after the pilot. Because cache keys include prompts and relevant references, affected chunks will be regenerated while unchanged work remains cached.

6. Translate production batches and assemble:

```bash
book-translate translate --start 3 --end 20
book-translate review --start 3 --end 20 --escalate
book-translate assemble
book-translate qa
```

QA also accepts `--start`, `--end`, and `--limit`, which is useful for validating a Codex pilot before the rest of the book is translated.

Reviewed rewrites are stored separately as `*.reviewed.zh-Hant.md`. Add `--apply` to the review command only when you want a complete reviewed passage copied over the draft. Assembly prefers the reviewed version when present.

## Cost and consistency controls

- Each model result has a content-derived cache key and usage metadata.
- Only glossary and character entries found in the current source chunk are included in its prompt.
- Only the tail of the previous translation and a compact continuity-memory tail are repeated.
- The strongest model receives only material escalations.
- `--start`, `--end`, and `--limit` make every model-backed stage resumable.
- `estimate` gives a conservative translation-only estimate; actual API usage is saved in adjacent metadata files.

Update `context/chapter_memory.md` at chapter boundaries. Keep it compact: continuity facts and unresolved references, not chapter retellings.

## PDF output

`output/book.zh-Hant.md` is the canonical assembled text. Keep the editable source canonical until linguistic review is complete; typeset and render the final PDF afterward, then visually inspect every page for broken glyphs, clipping, headers, footers, notes, and page transitions.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

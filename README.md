# Traditional Chinese book translation pipeline

This repository turns a PDF or UTF-8 ebook text file into a publication-quality Traditional Chinese (`zh-Hant-TW`) translation. It supports Codex with parallel subagents and a fully unattended OpenAI API mode. Both engines share the same durable task graph, provenance checks, ordered finalizer, formal quality gate, and resumable Notion sync.

Copyrighted PDFs, generated book text, API checkpoints, and SQLite run state are kept out of Git.

## Start a book

Install the project with Python 3.9 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
book-translate doctor
```

Create an isolated workspace and start the run:

```bash
book-translate init /path/to/book.pdf --project-dir books/my-book
# Native form-feed text is also supported:
book-translate init /path/to/book.txt --project-dir books/my-text-book
book-translate --config books/my-book/project.json run --engine codex --jobs 3
```

When this command is run as part of a Codex task, the repository's `AGENTS.md` operating contract makes Codex keep up to three subagents on independent work, replace completed workers with newly ready work, and preserve all progress in SQLite. The shell command alone cannot create Codex UI agents; it creates the durable queue that the active Codex task consumes.

To run unattended through the API instead:

```bash
export OPENAI_API_KEY="..."
book-translate --config books/my-book/project.json run --engine api --jobs 3
```

Codex mode does not require an API key. API mode does because it calls the Responses API outside the Codex app.

Resume after interruption or a quota reset, without repeating successful work:

```bash
book-translate --config books/my-book/project.json status --tasks
book-translate --config books/my-book/project.json resume
```

The current run is stored under `.book-translate/runs/<run-id>/`. Its immutable manifest fingerprints the source, config, prompts, and user-maintained references. Its SQLite queue records dependencies, leases, retries, hashes, model roles, token use, failures, and stale downstream work.

Text projects use `source_format: "text"` and `source_text: "input/book.txt"`. When an ebook export contains previews or appended back matter inside the same virtual page, set `source_line_start` and `source_line_end` for an exact, reproducible narrative boundary; the original text remains unchanged.

## Pipeline and parallelization

```text
inspect → extract → chunk → metadata + online verification
                            ↓
          terminology scans (parallel, ≤3)
                            ↓
                terminology consolidation
                            ↓
             draft translations (parallel, ≤3)
                            ↓
             bilingual reviews (parallel, ≤3)
                            ↓
          continuity finalizer (strict source order)
                            ↓
               assemble → formal QA → Notion
```

Drafting and bilingual review use immutable, compact packets containing only the chunk, neighbouring source tails, matching approved terminology, style/brief snapshot, and continuity memory. They do not resend the entire book. Finalization is deliberately sequential: chunk N consumes the finalized tail of chunk N−1, which prevents parallel workers from introducing unresolved voice and transition drift.

The default concurrency ceiling is three. More agents can reduce wall-clock time, but usually duplicate enough context and coordination tokens that they are less efficient without measured evidence.

## Model routing

The defaults are configuration, not hard-coded policy:

| Role | Default model | Reasoning | Use |
|---|---|---:|---|
| Metadata and one online verification pass | `gpt-5.6-luna` | low | Title, author, genre, audience, edition |
| Terminology discovery | `gpt-5.6-luna` | low | High-volume candidate extraction |
| Terminology consolidation | `gpt-5.6-sol` | medium | One global conflict-resolution pass |
| Draft translation | `gpt-5.6-terra` | low | Main quality/cost balance |
| Bilingual review | `gpt-5.6-terra` | medium | Source-target fidelity checks |
| Ordered continuity finalizer | `gpt-5.6-terra` | medium | Applies review and cross-chunk continuity |
| Consequential ambiguity | `gpt-5.6-sol` | high | Only review cases marked `escalate` |
| Final book audit, when enabled | `gpt-5.6-sol` | high | Selective release-level escalation |

Subagents do consume additional tokens because each needs instructions and local context. The pipeline limits that overhead with compact packets, consecutive chunk grouping, a three-worker ceiling, content-derived cache keys, and selective use of the strongest model. Parallelization primarily saves elapsed time; these controls keep its token premium bounded.

## Completion and invalidation rules

The formal release gate validates:

- exact source, translation, review, and finalizer hashes;
- current review provenance and acceptable verdicts;
- omissions via paragraph and chapter-heading structure;
- Simplified Chinese, English residue, placeholders, broken punctuation, and Markdown emphasis;
- every approved glossary form used in relevant chunks;
- exact assembly marker order and byte-for-byte chosen chunk content.

Paragraph-count exceptions must identify the chunk, expected counts, delta, and reason in `project.json`. A source, config, prompt, reference, or model change invalidates its stage and all downstream stages. Notion cannot run until assembly and formal QA succeed.

The individual diagnostic commands remain available when needed:

```bash
book-translate inspect
book-translate ocr        # only when the PDF is scanned
book-translate extract
book-translate chunk
book-translate estimate
book-translate terms
book-translate translate --jobs 3
book-translate review --jobs 3 --escalate
book-translate assemble
book-translate qa
```

## Notion library sync

Add `--notion` to `run` to make Notion the terminal task, or run it separately after QA:

```bash
python3 -m translation_pipeline.notion_sync plan --root books/my-book
python3 -m translation_pipeline.notion_sync sync \
  --root books/my-book \
  --env-file /path/to/.env \
  --parent-title "Book Translation"
```

The integration creates or reuses a `Books` database, upserts by a stable Book ID, and preserves an existing Read Status unless explicitly changed. Uploads are checkpointed in 100-block batches and verified by remote readback. Safe replacement deletes only verified pipeline-owned blocks, preserves manual blocks, and refuses ambiguous legacy content.

For an existing unmarked import, first run the new `sync` without `--replace-content`. That readback adopts the old body only if every block matches. A later changed translation may then use `--replace-content` safely.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile translation_pipeline/*.py
git diff --check
```

# Book translation operating contract

When the user supplies a book and asks to translate it, use the durable pipeline in this repository. Do not ask for an OpenAI API key when the selected engine is `codex`.

## Required workflow

1. Initialize a separate per-book project with `book-translate init SOURCE.pdf` unless the PDF is already the configured project source.
2. Start or resume the durable run. Use `book-translate run --engine codex --jobs 3` for Codex work and `book-translate resume` after interruption or quota reset. Never discard `.book-translate/runs/*/state.sqlite3`.
3. Keep up to three subagents active whenever independent terminology, draft-translation, or bilingual-review tasks are ready. Give each subagent disjoint consecutive chunk groups and an immutable context snapshot. Replace completed subagents with newly ready work until the parallel queues are empty.
4. Do not parallelize final continuity editing. Finalize chunks in source order because each finalizer consumes the preceding finalized target tail.
5. Record every task result, input/output hash, model role, and failure in the current `RunStore`. On a changed source, prompt, reference, config, or model, mark the affected task and all downstream tasks stale instead of silently reusing them.
6. Do not assemble, mark the book complete, or sync Notion until the formal quality gate passes. Notion is always the terminal task.

## Quality and token policy

- Preserve full meaning, paragraph structure, voice, ambiguity, and approved terminology. Use publication-quality Traditional Chinese for Taiwan and never Simplified Chinese.
- Parallel workers receive only their source chunks, neighbouring source tails, approved matching references, the style/brief snapshot, and a compact continuity memory. Do not send the full book to every worker.
- Use the configured low-cost model role for metadata and terminology, the balanced role for translation/review/finalization, and the strongest role only for terminology consolidation, consequential ambiguity, and escalations.
- Three concurrent workers is the default ceiling. Increasing agent count increases duplicated context tokens and must be justified by measured throughput without a quality regression.

## Progress handoff

Before ending a Codex turn for quota or usage reasons, run `book-translate status --tasks`, update `context/progress.md`, and leave current task leases in a recoverable state. On the next turn, resume the existing run rather than creating a new one.

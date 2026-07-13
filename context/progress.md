# Translation progress

This is the durable restart ledger for interactive Codex translation. Update it after every completed and reviewed chunk, and consolidate continuity memory at every chapter boundary.

## Current checkpoint

- Status: complete
- Last completed chunk: `chunk-0068`
- Last completed numbered chapter: Chapter Twenty
- Last completed section: Epilogue
- Next chunk: none
- Total source chunks: 68
- Completed chunks: 68
- Remaining chunks: 0
- Completed source range: pages 4-248
- Next source range: none
- Canonical complete translation: `output/book.zh-Hant.md`
- Final full-book QA: chunks 1-68, zero issues
- Final integrity audit: 68/68 source, translation, meta, and review hashes match; 21 headings present; complete assembly verified
- Final paragraph audit: 2,897 source paragraphs and 2,898 target paragraphs; the single expected delta is the documented chunk-0006 speaker split
- Last test run: 8/8 tests passed

## Completed production summary

- Workflow: parallel candidate translation, sequential primary-agent bilingual review and finalization.
- Formal completion rule: candidate drafts do not count as completed until copied into `output/chunks/`, paragraph-checked, hash-recorded, reviewed, and covered by QA.
- `chunk-0065`: primary bilingual review complete, 59/59 paragraphs, scoped QA zero issues.
- `chunk-0066`: primary bilingual review complete, 51/51 paragraphs, scoped QA zero issues.
- `chunk-0067`: primary bilingual review complete, 37/37 paragraphs, scoped QA zero issues; Chapter Twenty complete.
- `chunk-0068` (Epilogue): primary bilingual review complete, 21/21 paragraphs, scoped QA zero issues. Direct PDF page-248 verification confirms the repaired ending is complete and excludes the author biography.
- All 68 chunks are formally translated and reviewed. Full-book consistency corrections standardized Traditional Chinese classifier glyphs, Taiwan English-unit forms, and all chapter headings; affected hashes were updated.
- Parallel deep reviews, full-book integrity auditing, QA, tests, and complete assembly are finished.
- Source extraction covers PDF pages 4-248, retains the complete epilogue, and stops before `About the Author`.

## Verification procedure

1. Read `context/project_brief.md`, `context/style_guide.md`, `context/glossary.csv`, `context/characters.csv`, and `context/chapter_memory.md`.
2. Confirm the hashes in completed `output/chunks/*.meta.json` and `output/reviews/*.review.json`.
3. Re-run `python3 -m translation_pipeline.cli qa --start 1 --limit 68`, the unit tests, and `python3 -m translation_pipeline.cli assemble` after any future edit.
4. Recalculate and synchronize the affected translation hash in both meta and review records after any future canonical text edit.
5. Candidate files under `build/drafts/` are disposable working material; `output/chunks/` and the assembled book are canonical.

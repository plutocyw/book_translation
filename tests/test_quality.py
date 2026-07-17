import csv
import json
import tempfile
import unittest
from pathlib import Path

from translation_pipeline.quality import (
    QualityGateError,
    artifact_input_hash,
    assert_quality_gate,
    invalidation_plan,
    mark_stale,
    run_quality_gate,
)


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "build").mkdir()
        (self.root / "context").mkdir()
        (self.root / "output" / "chunks").mkdir(parents=True)
        (self.root / "output" / "reviews").mkdir()
        self.rows = [
            self._row("chunk-0001", 1, "One\n\nDiablo arrived.", 1, 2),
            self._row("chunk-0002", 2, "He left.", 2, 3),
        ]
        (self.root / "build" / "chunks.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8"
        )
        with (self.root / "context" / "glossary.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["source_term", "target_term", "status"])
            writer.writeheader()
            writer.writerow({"source_term": "Diablo", "target_term": "迪亞布羅", "status": "approved"})
        self.targets = {"chunk-0001": "# 一\n\n迪亞布羅抵達了。\n", "chunk-0002": "他離開了。\n"}
        for row in self.rows:
            self._write_artifacts(row, self.targets[row["chunk_id"]])
        self._assemble()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _row(chunk_id, index, source, start, end):
        from translation_pipeline.quality import sha256_text

        return {
            "chunk_id": chunk_id,
            "index": index,
            "page_start": start,
            "page_end": end,
            "source": source,
            "source_sha256": sha256_text(source),
        }

    def _write_artifacts(self, row, target):
        from translation_pipeline.quality import sha256_text

        chunk_id = row["chunk_id"]
        target_hash = sha256_text(target)
        (self.root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md").write_text(target, encoding="utf-8")
        self._json(
            self.root / "output" / "chunks" / f"{chunk_id}.meta.json",
            {"chunk_id": chunk_id, "source_sha256": row["source_sha256"], "translation_sha256": target_hash},
        )
        self._json(
            self.root / "output" / "reviews" / f"{chunk_id}.review.json",
            {"verdict": "pass", "issues": [], "source_sha256": row["source_sha256"], "translation_sha256": target_hash},
        )

    @staticmethod
    def _json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def _assemble(self):
        pieces = []
        for row in self.rows:
            chunk_id = row["chunk_id"]
            reviewed = self.root / "output" / "chunks" / f"{chunk_id}.reviewed.zh-Hant.md"
            normal = self.root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
            target = (reviewed if reviewed.exists() else normal).read_text(encoding="utf-8").strip()
            pieces.append(
                f"<!-- {chunk_id}; source pages {row['page_start']}-{row['page_end']} -->\n\n{target}"
            )
        (self.root / "output" / "book.zh-Hant.md").write_text("\n\n".join(pieces) + "\n", encoding="utf-8")

    def test_complete_current_artifacts_pass(self):
        report = assert_quality_gate(self.root)
        self.assertTrue(report.passed)
        self.assertEqual(report.to_dict()["checked_chunks"], 2)

    def test_hash_and_review_failures_are_structured_and_raise(self):
        review = self.root / "output" / "reviews" / "chunk-0001.review.json"
        value = json.loads(review.read_text())
        value.update({"verdict": "revise", "issues": ["missing sentence"], "translation_sha256": "stale"})
        self._json(review, value)
        report = run_quality_gate(self.root)
        codes = {issue.code for issue in report.issues}
        self.assertTrue({"review_not_passed", "review_issues_not_empty", "review_translation_hash_mismatch"} <= codes)
        with self.assertRaises(QualityGateError) as caught:
            assert_quality_gate(self.root)
        self.assertIs(caught.exception.report, caught.exception.report)

    def test_text_rules_catch_simplified_english_placeholder_and_markup(self):
        target = "# 一\n\n这是 a whole sentence that was left in English TODO 「未關閉。 *斜體\n"
        self._write_artifacts(self.rows[0], target)
        self._assemble()
        codes = {issue.code for issue in run_quality_gate(self.root).issues if issue.chunk_id == "chunk-0001"}
        self.assertTrue(
            {"simplified_chinese", "english_residue", "placeholder", "unbalanced_quotes_or_brackets", "unbalanced_emphasis"}
            <= codes
        )

    def test_paragraph_exception_must_be_specific_and_justified(self):
        target = "# 一\n\n迪亞布羅抵達了。\n\n這是刻意拆段。\n"
        self._write_artifacts(self.rows[0], target)
        self._assemble()
        bad = run_quality_gate(self.root)
        self.assertIn("paragraph_count_mismatch", {issue.code for issue in bad.issues})
        manifest = {
            "quality": {
                "paragraph_count_exceptions": {
                    "chunk-0001": {"source": 2, "target": 3, "delta": 1, "reason": "speaker split"}
                }
            }
        }
        self.assertTrue(run_quality_gate(self.root, manifest).passed)

    def test_assembled_book_must_exactly_match_marker_order_and_content(self):
        book = self.root / "output" / "book.zh-Hant.md"
        book.write_text(book.read_text().replace("chunk-0001", "chunk-0002", 1), encoding="utf-8")
        codes = {issue.code for issue in run_quality_gate(self.root).issues}
        self.assertIn("assembled_marker_order_mismatch", codes)
        self.assertIn("assembled_content_mismatch", codes)

    def test_reviewed_translation_requires_current_provenance(self):
        from translation_pipeline.quality import sha256_text

        row = self.rows[0]
        chunk_id = row["chunk_id"]
        target = self.targets[chunk_id]
        reviewed = self.root / "output" / "chunks" / f"{chunk_id}.reviewed.zh-Hant.md"
        reviewed.write_text(target, encoding="utf-8")
        target_hash = sha256_text(target)
        reviewed_meta = self.root / "output" / "chunks" / f"{chunk_id}.reviewed.meta.json"
        self._json(
            reviewed_meta,
            {
                "chunk_id": chunk_id,
                "source_sha256": row["source_sha256"],
                "translation_sha256": target_hash,
                "input_hash": "old",
            },
        )
        self._assemble()
        report = run_quality_gate(self.root, {"review_input_hashes": {chunk_id: "new"}})
        self.assertIn("reviewed_input_hash_mismatch", {issue.code for issue in report.issues})
        value = json.loads(reviewed_meta.read_text())
        value["input_hash"] = "new"
        self._json(reviewed_meta, value)
        self.assertTrue(run_quality_gate(self.root, {"review_input_hashes": {chunk_id: "new"}}).passed)

    def test_finalized_translation_binds_draft_and_review(self):
        from translation_pipeline.quality import sha256_text

        row = self.rows[0]
        chunk_id = row["chunk_id"]
        draft = self.root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
        review_path = self.root / "output" / "reviews" / f"{chunk_id}.review.json"
        review = json.loads(review_path.read_text())
        review.update({"verdict": "revise", "issues": [{"severity": "medium"}]})
        self._json(review_path, review)
        final_path = self.root / "output" / "chunks" / f"{chunk_id}.final.zh-Hant.md"
        final_path.write_text(self.targets[chunk_id], encoding="utf-8")
        self._json(
            self.root / "output" / "chunks" / f"{chunk_id}.final.meta.json",
            {
                "chunk_id": chunk_id,
                "source_sha256": row["source_sha256"],
                "draft_sha256": sha256_text(draft.read_text()),
                "review_sha256": sha256_text(review_path.read_text()),
                "translation_sha256": sha256_text(final_path.read_text()),
            },
        )
        self._assemble()
        self.assertTrue(assert_quality_gate(self.root).passed)

    def test_dependency_invalidation_propagates_through_stage_dag(self):
        plan = invalidation_plan(
            {"source": "a", "config": "c", "prompts": "p", "references": "r", "models": "m"},
            {"source": "a", "config": "c", "prompts": "p2", "references": "r", "models": "m2"},
        )
        self.assertEqual(plan["changed_dependencies"], ["models", "prompts"])
        self.assertEqual(plan["stale_stages"][0], "terms")
        self.assertEqual(plan["stale_stages"][-1], "notion")
        marked = mark_stale({"metadata": {"status": "complete"}, "terms": "complete", "draft": {}}, plan["stale_stages"])
        self.assertEqual(marked["metadata"]["status"], "complete")
        self.assertEqual(marked["terms"]["status"], "stale")
        self.assertEqual(marked["draft"]["status"], "stale")
        self.assertEqual(len(artifact_input_hash(a=1, b=2)), 64)

    def test_short_glossary_terms_require_source_word_boundaries(self):
        from translation_pipeline.quality import sha256_text

        with (self.root / "context" / "glossary.csv").open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["source_term", "target_term", "status"])
            writer.writerow({"source_term": "imp", "target_term": "小惡魔", "status": "approved"})
            writer.writerow({"source_term": "ward", "target_term": "防護結界", "status": "approved"})
        self.rows[1]["source"] = "He made a simple move forward."
        self.rows[1]["source_sha256"] = sha256_text(self.rows[1]["source"])
        (self.root / "build" / "chunks.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8"
        )
        self._write_artifacts(self.rows[1], self.targets["chunk-0002"])
        self._assemble()
        codes = {issue.code for issue in run_quality_gate(self.root).issues}
        self.assertNotIn("approved_term_missing", codes)


if __name__ == "__main__":
    unittest.main()

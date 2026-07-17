import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from translation_pipeline.runner import consolidated_term_candidates, create_pipeline_run, current_run, expected_output


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("build", "context", "prompts", "input"):
            (self.root / name).mkdir()
        (self.root / "input" / "book.txt").write_text("source", encoding="utf-8")
        for name in ("metadata", "terminology", "terminology_consolidate", "translate", "review", "finalize"):
            (self.root / "prompts" / f"{name}.md").write_text(name, encoding="utf-8")
        (self.root / "context" / "project_brief.md").write_text("brief", encoding="utf-8")
        (self.root / "context" / "style_guide.md").write_text("style", encoding="utf-8")
        (self.root / "context" / "glossary.csv").write_text(
            "source_term,target_term,category,status,first_chunk,notes\n", encoding="utf-8"
        )
        (self.root / "context" / "characters.csv").write_text(
            "source_name,target_name,aliases,pronouns_or_gender,role,status,notes\n", encoding="utf-8"
        )
        rows = [
            {"chunk_id": f"chunk-{index:04d}", "index": index, "source": f"source {index}"}
            for index in (1, 2)
        ]
        (self.root / "build" / "chunks.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        self.config_path = self.root / "project.json"
        self.config_path.write_text("{}", encoding="utf-8")
        models = {
            "metadata": "luna",
            "terminology": "luna",
            "terminology_consolidate": "sol",
            "translate": "terra",
            "review": "terra",
            "finalize": "terra",
            "book_audit": "sol",
            "adjudicate": "sol",
        }
        self.cfg = {
            "_config_path": str(self.config_path),
            "source_text": "input/book.txt",
            "source_format": "text",
            "models": models,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_graph_parallelizes_candidates_and_serializes_finalization(self):
        run = create_pipeline_run(self.root, self.cfg, engine="codex", jobs=3, run_id="test", notion=True)
        self.assertEqual(run.status()["total_tasks"], 14)
        self.assertEqual([task["task_id"] for task in run.tasks("ready")], ["metadata"])
        with sqlite3.connect(run.database_path) as connection:
            dependencies = set(connection.execute("SELECT task_id, depends_on FROM task_dependencies"))
        self.assertIn(("finalize:chunk-0002", "review:chunk-0002"), dependencies)
        self.assertIn(("finalize:chunk-0002", "finalize:chunk-0001"), dependencies)
        self.assertIn(("quality", "assemble"), dependencies)
        self.assertIn(("book-audit", "quality"), dependencies)
        self.assertIn(("notion", "book-audit"), dependencies)
        self.assertEqual(current_run(self.root).run_id, "test")
        self.assertTrue(str(expected_output(self.root, run.task("translate:chunk-0001"))).endswith("chunk-0001.zh-Hant.md"))

    def test_term_candidates_are_deduplicated_before_global_consolidation(self):
        terms = self.root / "build" / "terms"
        terms.mkdir()
        (terms / "chunk-0001.json").write_text(
            json.dumps({"terms": [{"source_term": "Trag’Oul", "proposed_target": "塔格奧", "category": "character", "confidence": "high", "notes": "official"}]}),
            encoding="utf-8",
        )
        (terms / "chunk-0002.json").write_text(
            json.dumps({"terms": [{"source_term": "Trag'Oul", "proposed_target": "塔格奧", "category": "character", "confidence": "high", "notes": "official"}]}),
            encoding="utf-8",
        )
        candidates = consolidated_term_candidates(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["mentions"], 2)
        self.assertEqual(candidates[0]["candidate_targets"], {"塔格奧": 2})


if __name__ == "__main__":
    unittest.main()

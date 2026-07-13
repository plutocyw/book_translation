import tempfile
import time
import unittest
from pathlib import Path

from translation_pipeline.workers import (
    bounded_parallel_map,
    group_consecutive,
    immutable_translation_input,
    make_context_snapshot,
    neighbor_source_context,
)


class WorkerTests(unittest.TestCase):
    def _root(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "context").mkdir()
        (root / "context" / "project_brief.md").write_text("Brief", encoding="utf-8")
        (root / "context" / "style_guide.md").write_text("Style", encoding="utf-8")
        (root / "context" / "chapter_memory.md").write_text("Memory", encoding="utf-8")
        return temp, root

    def test_snapshot_and_packet_are_immutable(self):
        temp, root = self._root()
        self.addCleanup(temp.cleanup)
        snapshot = make_context_snapshot(root, prompt_text="Prompt", reference_text="Refs")
        chunks = [
            {"chunk_id": "chunk-0001", "page_start": 1, "page_end": 2, "source": "Alpha"},
            {"chunk_id": "chunk-0002", "page_start": 2, "page_end": 3, "source": "Beta"},
        ]
        packet = immutable_translation_input(snapshot, chunks, 1, "Term -> 術語")
        (root / "context" / "chapter_memory.md").write_text("Changed", encoding="utf-8")
        self.assertIn("Memory", packet)
        self.assertNotIn("Changed", packet)
        self.assertIn("Alpha", packet)

    def test_neighbor_context(self):
        chunks = [{"source": "one"}, {"source": "two"}, {"source": "three"}]
        self.assertEqual(neighbor_source_context(chunks, 1), ("one", "three"))

    def test_parallel_map_preserves_order(self):
        def worker(value):
            time.sleep((4 - value) * 0.005)
            return value * 2

        self.assertEqual(bounded_parallel_map([1, 2, 3], worker, jobs=3), [2, 4, 6])

    def test_group_consecutive(self):
        self.assertEqual(group_consecutive([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])


if __name__ == "__main__":
    unittest.main()

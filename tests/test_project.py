import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_pipeline.project import initialize_project, slugify


class ProjectTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("My Great Book!"), "my-great-book")

    @patch("translation_pipeline.project.pdf_metadata")
    def test_initialize_isolated_project(self, metadata):
        metadata.return_value = {"title": "Example", "author": "Writer", "pages": 123}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"pdf")
            template = root / "template"
            (template / "prompts").mkdir(parents=True)
            (template / "prompts" / "translate.md").write_text("Translate", encoding="utf-8")
            project = root / "books" / "example"
            config = initialize_project(source, project, template)
            self.assertTrue(config.exists())
            self.assertTrue((project / "input" / "book.pdf").exists())
            self.assertIn("Example", (project / "context" / "project_brief.md").read_text())
            self.assertIn('"source_page_end": 123', config.read_text())


if __name__ == "__main__":
    unittest.main()

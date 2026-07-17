import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_pipeline.project import initialize_project, slugify, text_metadata


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

    def test_initialize_text_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "novel.txt"
            source.write_text("Page one.\fPage two.\fPage three.", encoding="utf-8")
            template = root / "template"
            (template / "prompts").mkdir(parents=True)
            project = root / "books" / "novel"

            config_path = initialize_project(source, project, template)
            config_text = config_path.read_text(encoding="utf-8")

            self.assertEqual((project / "input" / "book.txt").read_text(), source.read_text())
            self.assertIn('"source_text": "input/book.txt"', config_text)
            self.assertIn('"source_format": "text"', config_text)
            self.assertIn('"source_page_end": 3', config_text)
            self.assertNotIn('"source_pdf"', config_text)
            self.assertIn("UTF-8 text supplied", (project / "context" / "project_brief.md").read_text())

    def test_text_metadata_ignores_empty_trailing_form_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("one\ftwo\f\n", encoding="utf-8")
            self.assertEqual(text_metadata(source)["pages"], 2)


if __name__ == "__main__":
    unittest.main()

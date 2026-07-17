import tempfile
import unittest
from pathlib import Path

from translation_pipeline.cli import (
    configured_source,
    is_chapter_heading,
    is_stop_heading,
    make_chunks,
    normalize_page_text,
    page_text_from_positioned_lines,
    parse_json_response,
    text_page_records,
    word_count,
)


class PipelineTests(unittest.TestCase):
    def test_normalize_page_text_joins_wrapped_lines_and_hyphens(self):
        raw = "A para-\ngraph wraps here.\nStill same block.\n\nSecond block."
        self.assertEqual(
            normalize_page_text(raw),
            "A paragraph wraps here. Still same block.\n\nSecond block.",
        )

    def test_make_chunks_preserves_source_order(self):
        pages = [
            {"page": 1, "text": "one two three four\n\nfive six seven eight"},
            {"page": 2, "text": "nine ten eleven twelve"},
        ]
        chunks = make_chunks(pages, target=5, maximum=8)
        joined = " ".join(chunk["source"].replace("\n\n", " ") for chunk in chunks)
        self.assertEqual(joined, "one two three four five six seven eight nine ten eleven twelve")
        self.assertEqual(sum(chunk["source_words"] for chunk in chunks), 12)

    def test_positioned_lines_remove_converter_header_and_preserve_paragraphs(self):
        text, continues = page_text_from_positioned_lines(
            [
                {"text": "ABC Amber LIT Converter", "x0": 90, "top": 36},
                {"text": "http://www.processtext.com/abclit.html", "x0": 90, "top": 50},
                {"text": "First paragraph wraps", "x0": 93, "top": 100},
                {"text": "onto another line.", "x0": 90, "top": 114},
                {"text": "Second paragraph.", "x0": 93, "top": 142},
            ]
        )
        self.assertFalse(continues)
        self.assertEqual(text, "First paragraph wraps onto another line.\n\nSecond paragraph.")

    def test_positioned_lines_do_not_split_misindented_wrapped_text(self):
        text, _ = page_text_from_positioned_lines(
            [
                {"text": "A sentence ends its line with", "x0": 93, "top": 100},
                {"text": "a misindented continuation.", "x0": 93, "top": 128},
                {"text": "A real new paragraph.", "x0": 93, "top": 156},
            ]
        )
        self.assertEqual(
            text,
            "A sentence ends its line with a misindented continuation.\n\nA real new paragraph.",
        )

    def test_make_chunks_joins_page_continuation(self):
        pages = [
            {"page": 4, "text": "A sentence begins", "continues_from_previous": False},
            {"page": 5, "text": "and ends here.\n\nNext paragraph.", "continues_from_previous": True},
        ]
        chunks = make_chunks(pages, target=100, maximum=100)
        self.assertEqual(chunks[0]["source"], "A sentence begins and ends here.\n\nNext paragraph.")
        self.assertEqual((chunks[0]["page_start"], chunks[0]["page_end"]), (4, 5))

    def test_make_chunks_never_crosses_chapter_heading(self):
        pages = [
            {"page": 16, "text": "End of chapter one."},
            {"page": 17, "text": "Two\n\nStart of chapter two."},
        ]
        chunks = make_chunks(pages, target=100, maximum=100)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["source"], "End of chapter one.")
        self.assertTrue(chunks[1]["source"].startswith("Two\n\n"))

    def test_json_fence_is_accepted(self):
        self.assertEqual(parse_json_response('```json\n{"terms": []}\n```'), {"terms": []})

    def test_word_count_handles_apostrophes(self):
        self.assertEqual(word_count("It's the author's book."), 4)

    def test_generic_chapter_headings(self):
        for value in ("Chapter 1", "CHAPTER XII", "Part Two", "Prologue", "Thirty-One", "ONE", "TWENTY-THREE"):
            self.assertTrue(is_chapter_heading(value), value)
        self.assertFalse(is_chapter_heading("He entered chapter one of his life."))
        self.assertTrue(is_stop_heading("ABOUT THEAUTHOR"))

    def test_text_page_records_use_form_feed_and_requested_page_range(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text(
                "First paragraph continues\fonto page two.\n\nSecond paragraph.\fChapter 2\n\nOpening.",
                encoding="utf-8",
            )
            pages = text_page_records(source, page_start=2, page_end=3)

            self.assertEqual([row["page"] for row in pages], [2, 3])
            self.assertFalse(pages[0]["continues_from_previous"])
            self.assertFalse(pages[1]["continues_from_previous"])
            self.assertEqual(pages[0]["sha256"], text_page_records(source, 2, 2)[0]["sha256"])

    def test_text_page_records_mark_mid_paragraph_page_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("A sentence continues\fon the next page.\n\n", encoding="utf-8")
            pages = text_page_records(source)
            self.assertTrue(pages[1]["continues_from_previous"])

    def test_text_page_after_chapter_heading_is_not_joined_to_heading(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("FOUR\fOpening chapter paragraph.", encoding="utf-8")
            pages = text_page_records(source)
            self.assertFalse(pages[1]["continues_from_previous"])

    def test_configured_source_supports_native_text_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = {
                "_config_path": str(root / "project.json"),
                "source_format": "text",
                "source_text": "input/book.txt",
            }
            self.assertEqual(configured_source(cfg), (root / "input" / "book.txt", "text"))

    def test_text_line_bounds_preserve_original_page_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("teaser\f\nONE\n\nStory.\f\nTWO\n\nMore.\f\nABOUT THEAUTHOR", encoding="utf-8")
            pages = text_page_records(source, line_start=2, line_end=7)
            self.assertEqual([row["page"] for row in pages], [2, 3])
            self.assertTrue(pages[0]["text"].startswith("ONE"))


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from translation_pipeline.cli import (
    is_chapter_heading,
    make_chunks,
    normalize_page_text,
    page_text_from_positioned_lines,
    parse_json_response,
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
        for value in ("Chapter 1", "CHAPTER XII", "Part Two", "Prologue", "Thirty-One"):
            self.assertTrue(is_chapter_heading(value), value)
        self.assertFalse(is_chapter_heading("He entered chapter one of his life."))


if __name__ == "__main__":
    unittest.main()

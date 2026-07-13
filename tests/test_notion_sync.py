import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from translation_pipeline.notion_sync import (
    NotionError,
    batch_manifest,
    book_properties,
    database_schema,
    find_pipeline_batches,
    inline_to_rich_text,
    load_state,
    parse_project_metadata,
    save_state,
    sync_book,
    translated_book_blocks,
)


def metadata(book_id="book-v1-test"):
    return {
        "book_id": book_id,
        "book_name": "Test Book",
        "author": "Test Author",
        "genre": "Fantasy",
        "audience": "Adults",
        "publication_year": 2025,
        "isbn": "123",
        "source_language": "English",
        "translation_language": "Traditional Chinese (Taiwan)",
        "translation_status": "Complete",
        "chapter_count": 1,
        "chunk_count": 1,
        "completed_at": "2025-01-02",
        "repository": None,
        "schema_version": 2,
    }


class FakeClient:
    def __init__(self, page=None, children=None):
        self.page = deepcopy(page)
        self.children = deepcopy(children or [])
        self.next_id = 1
        self.content_append_calls = 0
        self.fail_content_call = None

    def _with_ids(self, blocks):
        result = []
        for block in deepcopy(blocks):
            block.setdefault("id", f"block-{self.next_id}")
            self.next_id += 1
            result.append(block)
        return result

    def query_book(self, data_source_id, book_id, title):
        return [deepcopy(self.page)] if self.page else []

    def create_book_page(self, data_source_id, properties):
        self.page = {"id": "page-1", "url": "https://notion.test/page-1", "properties": deepcopy(properties)}
        return deepcopy(self.page)

    def update_page_properties(self, page_id, properties):
        self.page["properties"].update(deepcopy(properties))
        return deepcopy(self.page)

    def list_children(self, page_id):
        return deepcopy(self.children)

    def append_blocks(self, page_id, blocks):
        is_marker = len(blocks) == 1 and "book-translation-pipeline:batch:" in str(blocks)
        if not is_marker:
            self.content_append_calls += 1
            if self.fail_content_call == self.content_append_calls:
                raise RuntimeError("simulated interruption")
        self.children.extend(self._with_ids(blocks))

    def delete_block(self, block_id):
        self.children = [block for block in self.children if block.get("id") != block_id]

    def request(self, method, path, payload=None):
        if method == "GET" and path.startswith("/pages/"):
            return deepcopy(self.page)
        raise AssertionError((method, path, payload))


class NotionSyncTests(unittest.TestCase):
    def test_schema_has_identity_version_and_readback_properties(self):
        schema = database_schema()
        self.assertIn("title", schema["Book"])
        self.assertIn("rich_text", schema["Book ID"])
        self.assertIn("number", schema["Schema Version"])
        self.assertIn("select", schema["Read Status"])
        self.assertIn("rich_text", schema["Content Hash"])
        self.assertIn("number", schema["Imported Blocks"])

    def test_book_blocks_skip_chunk_markers_and_preserve_structure(self):
        markdown = """<!-- chunk-0001; source pages 4-7 -->

# 一

這是*測試*。

* * *
"""
        blocks = translated_book_blocks(markdown, {"book_name": "Book", "author": "Author"})
        types = [block["type"] for block in blocks]
        self.assertEqual(types[:3], ["callout", "table_of_contents", "divider"])
        self.assertEqual(types[3:], ["heading_1", "paragraph", "divider"])
        paragraph_runs = blocks[4]["paragraph"]["rich_text"]
        self.assertTrue(any(run["annotations"]["italic"] for run in paragraph_runs))

    def test_long_rich_text_is_split(self):
        runs = inline_to_rich_text("x" * 4500)
        self.assertEqual([len(run["text"]["content"]) for run in runs], [2000, 2000, 500])

    def test_project_metadata_uses_only_supplied_paths_and_has_stable_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "brief.md").write_text(
                "\n".join(
                    [
                        "- Book: *Custom Book*",
                        "- Author: Custom Author",
                        "- Source edition: Second edition; copyright 2024; ISBN `978-1-2`",
                        "- Genre: Fantasy",
                        "- Primary audience: Adults",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "source.pdf").write_bytes(b"unique source")
            (root / "project.json").write_text(
                json.dumps(
                    {
                        "source_pdf": "source.pdf",
                        "source_language": "English",
                        "target_locale": "zh-Hant-TW",
                    }
                ),
                encoding="utf-8",
            )
            (root / "book.md").write_text("# 一\n\n內容", encoding="utf-8")
            (root / "progress.md").write_text(
                "- Status: complete\n- Completed chunks: 7\n- Completed at: 2025-04-03\n",
                encoding="utf-8",
            )
            value = parse_project_metadata(
                Path("brief.md"), Path("project.json"), Path("book.md"), Path("progress.md"), root
            )
            again = parse_project_metadata(
                Path("brief.md"), Path("project.json"), Path("book.md"), Path("progress.md"), root
            )
            self.assertEqual(value["book_name"], "Custom Book")
            self.assertEqual(value["publication_year"], 2024)
            self.assertEqual(value["isbn"], "978-1-2")
            self.assertEqual(value["source_hash"], hashlib.sha256(b"unique source").hexdigest())
            self.assertEqual(value["chapter_count"], 1)
            self.assertEqual(value["chunk_count"], 7)
            self.assertEqual(value["completed_at"], "2025-04-03")
            self.assertEqual(value["book_id"], again["book_id"])

    def test_read_status_is_omitted_unless_explicit(self):
        props = book_properties(metadata(), None)
        self.assertNotIn("Read Status", props)
        self.assertEqual(book_properties(metadata(), "Reading")["Read Status"]["select"]["name"], "Reading")

    def test_state_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            expected = {"parent_page_id": "parent", "data_source_id": "source"}
            save_state(path, expected)
            self.assertEqual(load_state(path), expected)

    def test_sync_checkpoints_batches_and_preserves_existing_read_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "book.md"
            book.write_text("# 一\n\n內容", encoding="utf-8")
            state = root / "state.json"
            page = {
                "id": "page-1",
                "url": "https://notion.test/page-1",
                "properties": {
                    "Book": {"title": [{"text": {"content": "Test Book"}}]},
                    "Book ID": {"rich_text": [{"text": {"content": "book-v1-test"}}]},
                    "Read Status": {"select": {"name": "Reading"}},
                },
            }
            client = FakeClient(page=page)
            result = sync_book(client, "source", book, metadata(), None, False, state)
            self.assertTrue(result["uploaded"])
            self.assertEqual(client.page["properties"]["Read Status"]["select"]["name"], "Reading")
            upload = load_state(state)["uploads"]["book-v1-test"]
            self.assertEqual(upload["status"], "complete")
            self.assertEqual(upload["next_batch"], len(upload["batches"]))
            found = find_pipeline_batches(client.children, "book-v1-test")
            self.assertEqual(len(found), len(batch_manifest(translated_book_blocks(book.read_text(), metadata()))))

    def test_matching_translation_hash_still_requires_block_readback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "book.md"
            book.write_text("# 一\n\n內容", encoding="utf-8")
            state = root / "state.json"
            client = FakeClient()
            sync_book(client, "source", book, metadata(), None, False, state)
            client.children[1][client.children[1]["type"]]["rich_text"][0]["text"]["content"] = "corrupt"
            with self.assertRaisesRegex(NotionError, "does not match its checkpoint"):
                sync_book(client, "source", book, metadata(), None, False, state)

    def test_replace_deletes_only_verified_pipeline_blocks_and_keeps_manual_block(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "book.md"
            book.write_text("# 一\n\n舊內容", encoding="utf-8")
            state = root / "state.json"
            client = FakeClient()
            sync_book(client, "source", book, metadata(), None, False, state)
            manual = client._with_ids(
                [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": inline_to_rich_text("人工註記")}}]
            )[0]
            client.children.append(manual)
            book.write_text("# 一\n\n新內容", encoding="utf-8")
            sync_book(client, "source", book, metadata(), None, True, state)
            self.assertIn(manual["id"], [block["id"] for block in client.children])
            self.assertEqual(len(find_pipeline_batches(client.children, "book-v1-test")), 1)

    def test_replace_refuses_legacy_unmarked_body(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "book.md"
            book.write_text("# 一\n\n新內容", encoding="utf-8")
            page = {
                "id": "page-1",
                "properties": {
                    "Book": {"title": [{"text": {"content": "Test Book"}}]},
                    "Book ID": {"rich_text": [{"text": {"content": "book-v1-test"}}]},
                    "Translation Hash": {"rich_text": [{"text": {"content": "old"}}]},
                },
            }
            manual = [{"id": "manual", "type": "paragraph", "paragraph": {"rich_text": inline_to_rich_text("人工內容")}}]
            client = FakeClient(page, manual)
            with self.assertRaisesRegex(NotionError, "no verified legacy checkpoint"):
                sync_book(client, "source", book, metadata(), None, True, root / "state.json")
            self.assertEqual(client.children[0]["id"], "manual")

    def test_verified_legacy_import_can_be_adopted_then_safely_replaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "book.md"
            book.write_text("# 一\n\n舊內容", encoding="utf-8")
            digest = hashlib.sha256(book.read_text(encoding="utf-8").encode()).hexdigest()
            page = {
                "id": "page-1",
                "properties": {
                    "Book": {"title": [{"text": {"content": "Test Book"}}]},
                    "Read Status": {"select": {"name": "Read"}},
                    "Translation Hash": {"rich_text": [{"text": {"content": digest}}]},
                },
            }
            client = FakeClient(page)
            client.children = client._with_ids(translated_book_blocks(book.read_text(), metadata()))
            state = root / "state.json"
            adopted = sync_book(client, "source", book, metadata(), None, False, state)
            self.assertFalse(adopted["uploaded"])
            manual = client._with_ids(
                [{"type": "paragraph", "paragraph": {"rich_text": inline_to_rich_text("人工註記")}}]
            )[0]
            client.children.append(manual)
            book.write_text("# 一\n\n新內容", encoding="utf-8")
            sync_book(client, "source", book, metadata(), None, True, state)
            self.assertIn(manual["id"], [block["id"] for block in client.children])
            self.assertEqual(client.page["properties"]["Read Status"]["select"]["name"], "Read")

    def test_interrupted_upload_resumes_after_last_verified_batch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "book.md"
            book.write_text("\n\n".join(f"段落 {index}" for index in range(205)), encoding="utf-8")
            state = root / "state.json"
            client = FakeClient()
            client.fail_content_call = 2
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                sync_book(client, "source", book, metadata(), None, False, state)
            self.assertEqual(load_state(state)["uploads"]["book-v1-test"]["next_batch"], 1)
            client.fail_content_call = None
            result = sync_book(client, "source", book, metadata(), None, False, state)
            self.assertTrue(result["uploaded"])
            self.assertEqual(load_state(state)["uploads"]["book-v1-test"]["status"], "complete")
            self.assertEqual(len(find_pipeline_batches(client.children, "book-v1-test")), 3)


if __name__ == "__main__":
    unittest.main()

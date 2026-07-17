#!/usr/bin/env python3
"""Create a Notion books database and sync the canonical translated book.

This module follows the direct REST pattern used by ``newsletter/manny/notion_port.py``
while targeting Notion API version 2026-03-11 and its database/data-source split.
It is deliberately idempotent: setup reuses the database, and sync upserts by a
stable source/edition/locale Book ID (with a one-time title fallback for migration).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
DEFAULT_STATE = Path(".notion-state.json")
DEFAULT_BOOK = Path("output/book.zh-Hant.md")
DEFAULT_BRIEF = Path("context/project_brief.md")
DEFAULT_PROJECT = Path("project.json")
DEFAULT_PROGRESS = Path("context/progress.md")

SCHEMA_VERSION = 2
UPLOAD_BATCH_SIZE = 100
PIPELINE_MARKER_PREFIX = "book-translation-pipeline:batch:"

INLINE_RE = re.compile(r"\*\*(?P<bold>.+?)\*\*|\*(?P<italic>[^*\n]+?)\*")
CHUNK_MARKER_RE = re.compile(r"^<!--\s*chunk-\d{4};.*-->$")


class NotionError(RuntimeError):
    """Raised for actionable Notion API or workspace errors."""


def load_token(env_file: Optional[Path] = None) -> str:
    for name in ("NOTION_TOKEN", "NOTION_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()

    candidates = [env_file] if env_file else [Path(".env")]
    for path in candidates:
        if not path or not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {"NOTION_TOKEN", "NOTION_API_KEY"}:
                value = value.strip().strip("\"'")
                if value:
                    return value
    location = str(env_file) if env_file else ".env or the environment"
    raise NotionError(f"NOTION_TOKEN/NOTION_API_KEY not found in {location}")


def plain_text(content: str, **annotations: bool) -> Dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": content},
        "annotations": {
            "bold": bool(annotations.get("bold")),
            "italic": bool(annotations.get("italic")),
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
        },
    }


def inline_to_rich_text(text: str) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    pos = 0
    for match in INLINE_RE.finditer(text):
        if match.start() > pos:
            runs.extend(_split_run(text[pos : match.start()]))
        if match.group("bold") is not None:
            runs.extend(_split_run(match.group("bold"), bold=True))
        else:
            runs.extend(_split_run(match.group("italic"), italic=True))
        pos = match.end()
    if pos < len(text):
        runs.extend(_split_run(text[pos:]))
    return runs or [plain_text("")]


def _split_run(text: str, **annotations: bool) -> List[Dict[str, Any]]:
    # Notion limits each rich-text content value to 2,000 characters.
    return [plain_text(text[i : i + 2000], **annotations) for i in range(0, len(text), 2000)] or [
        plain_text("", **annotations)
    ]


def translated_book_blocks(markdown: str, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert the assembled Markdown book to Notion blocks.

    Chunk provenance comments are intentionally omitted. Chapter headings, emphasis,
    paragraphs, and scene dividers are retained.
    """
    blocks: List[Dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📖"},
                "rich_text": inline_to_rich_text(
                    f"Traditional Chinese translation of {metadata['book_name']} by {metadata['author']}."
                ),
            },
        },
        {"object": "block", "type": "table_of_contents", "table_of_contents": {}},
        {"object": "block", "type": "divider", "divider": {}},
    ]

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", markdown.strip()) if part.strip()]
    for paragraph in paragraphs:
        if CHUNK_MARKER_RE.match(paragraph):
            continue
        if paragraph == "* * *":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", paragraph)
        if heading:
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_1",
                    "heading_1": {"rich_text": inline_to_rich_text(heading.group(1))},
                }
            )
            continue
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": inline_to_rich_text(paragraph)},
            }
        )
    return blocks


def _rooted(root_path: Path, path: Path) -> Path:
    return path if path.is_absolute() else root_path / path


def _file_sha256(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_project_metadata(
    brief_path: Path,
    project_path: Path,
    book_path: Path,
    progress_path: Path,
    root_path: Path,
) -> Dict[str, Any]:
    """Read metadata exclusively from the paths supplied for this run."""
    root_path = root_path.resolve()
    brief_path = _rooted(root_path, brief_path)
    project_path = _rooted(root_path, project_path)
    book_path = _rooted(root_path, book_path)
    progress_path = _rooted(root_path, progress_path)
    brief = brief_path.read_text(encoding="utf-8")
    project = json.loads(project_path.read_text(encoding="utf-8"))
    inferred_path = root_path / "context" / "book_metadata.json"
    inferred = json.loads(inferred_path.read_text(encoding="utf-8")) if inferred_path.exists() else {}
    inferred_fields = {
        "Book": inferred.get("title"),
        "Author": inferred.get("author"),
        "Genre": inferred.get("genre"),
        "Primary audience": inferred.get("audience"),
        "Source edition": inferred.get("source_edition"),
    }

    def field(name: str, required: bool = True) -> Optional[str]:
        match = re.search(rf"^- {re.escape(name)}:\s*(.+)$", brief, flags=re.MULTILINE)
        if not match:
            if required:
                raise ValueError(f"Missing '{name}' in {brief_path}")
            return None
        value = match.group(1).strip().strip("*")
        fallback = inferred_fields.get(name)
        return str(fallback).strip() if fallback and value.casefold().startswith("todo") else value

    source_edition = field("Source edition") or ""
    year_match = re.search(r"(?:copyright\s*)?(\d{4})", source_edition, flags=re.IGNORECASE)
    isbn_match = re.search(r"ISBN\s*`?([^`;]+)`?", source_edition, flags=re.IGNORECASE)
    progress = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
    completed_match = re.search(r"Completed chunks:\s*(\d+)", progress)
    book_text = book_path.read_text(encoding="utf-8")
    chapter_count = len(re.findall(r"^# [一二三四五六七八九十]+$", book_text, re.MULTILINE))

    source_value = project.get("source_pdf") or project.get("source_text") or project.get("source_file")
    source_path = _rooted(root_path, Path(source_value)) if source_value else None
    source_hash = _file_sha256(source_path) if source_path else None
    completed_at_match = re.search(r"Completed (?:at|on):\s*(\d{4}-\d{2}-\d{2})", progress, re.IGNORECASE)
    is_complete = bool(re.search(r"^- Status:\s*complete\s*$", progress, re.MULTILINE | re.IGNORECASE))
    completed_at = completed_at_match.group(1) if completed_at_match else None

    target_locale = project.get("target_locale", "zh-Hant-TW")
    target_language = "Traditional Chinese (Taiwan)" if target_locale == "zh-Hant-TW" else target_locale
    metadata = {
        "book_name": field("Book"),
        "author": field("Author"),
        "genre": field("Genre"),
        "audience": field("Primary audience"),
        "source_edition": source_edition,
        "publication_year": int(year_match.group(1)) if year_match else None,
        "isbn": isbn_match.group(1).strip().strip("`") if isbn_match else None,
        "source_hash": source_hash,
        "source_language": project.get("source_language", "English"),
        "target_locale": target_locale,
        "translation_language": target_language,
        "translation_status": "Complete" if is_complete else "In Progress",
        "chapter_count": chapter_count,
        "chunk_count": int(completed_match.group(1)) if completed_match else None,
        "completed_at": completed_at,
        "repository": repository_url(root_path),
        "schema_version": SCHEMA_VERSION,
    }
    identity = {
        "isbn": metadata["isbn"],
        "source_edition": metadata["source_edition"],
        "source_hash": metadata["source_hash"],
        "target_locale": metadata["target_locale"],
    }
    metadata["book_id"] = "book-v1-" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return metadata


def repository_url(root_path: Path) -> Optional[str]:
    try:
        value = subprocess.check_output(
            ["git", "-C", str(root_path), "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.split(":", 1)[1]
    return value[:-4] if value.endswith(".git") else value


def database_schema() -> Dict[str, Any]:
    return {
        "Book": {"title": {}},
        "Book ID": {"rich_text": {}},
        "Schema Version": {"number": {"format": "number"}},
        "Author": {"rich_text": {}},
        "Read Status": {
            "select": {
                "options": [
                    {"name": "Not Started", "color": "gray"},
                    {"name": "Reading", "color": "blue"},
                    {"name": "Read", "color": "green"},
                    {"name": "On Hold", "color": "yellow"},
                ]
            }
        },
        "Translation Status": {
            "select": {
                "options": [
                    {"name": "In Progress", "color": "yellow"},
                    {"name": "Complete", "color": "green"},
                ]
            }
        },
        "Import Status": {
            "select": {
                "options": [
                    {"name": "Uploading", "color": "yellow"},
                    {"name": "Complete", "color": "green"},
                    {"name": "Failed", "color": "red"},
                ]
            }
        },
        "Genre": {"rich_text": {}},
        "Audience": {"rich_text": {}},
        "Source Language": {"select": {}},
        "Translation Language": {"select": {}},
        "Publication Year": {"number": {"format": "number"}},
        "ISBN": {"rich_text": {}},
        "Chapters": {"number": {"format": "number"}},
        "Chunks": {"number": {"format": "number"}},
        "Completed At": {"date": {}},
        "Translation Hash": {"rich_text": {}},
        "Content Hash": {"rich_text": {}},
        "Imported Blocks": {"number": {"format": "number"}},
        "Repository": {"url": {}},
    }


class NotionClient:
    def __init__(self, token: str, timeout: int = 60):
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = API + path
        for attempt in range(6):
            response = requests.request(
                method, url, headers=self.headers, json=payload, timeout=self.timeout
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 5:
                    break
                wait = float(response.headers.get("Retry-After", min(2**attempt, 10)))
                time.sleep(wait)
                continue
            if response.ok:
                return response.json() if response.content else {}
            raise NotionError(self._error_message(response))
        raise NotionError(self._error_message(response))

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            data = response.json()
            detail = data.get("message") or json.dumps(data, ensure_ascii=False)
        except ValueError:
            detail = response.text[:500]
        return f"Notion API {response.status_code}: {detail}"

    def search(self, query: str, object_type: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        payload: Dict[str, Any] = {
            "query": query,
            "filter": {"property": "object", "value": object_type},
            "page_size": 100,
        }
        while True:
            data = self.request("POST", "/search", payload)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            payload["start_cursor"] = data["next_cursor"]

    def find_parent_page(self, title: str, page_id: Optional[str] = None) -> Dict[str, Any]:
        if page_id:
            return self.request("GET", f"/pages/{page_id}")
        exact = [page for page in self.search(title, "page") if object_title(page) == title]
        if not exact:
            raise NotionError(
                f"No accessible Notion page titled '{title}'. Share the page with the integration, "
                "or pass --parent-page-id."
            )
        if len(exact) > 1:
            ids = ", ".join(page["id"] for page in exact)
            raise NotionError(f"Multiple pages titled '{title}' are accessible ({ids}); pass --parent-page-id.")
        return exact[0]

    def find_data_source(self, parent_page_id: str, database_title: str) -> Optional[Dict[str, Any]]:
        matches = []
        for item in self.search(database_title, "data_source"):
            if object_title(item) != database_title:
                continue
            parent = item.get("database_parent") or {}
            if parent.get("page_id") == parent_page_id:
                matches.append(item)
        if len(matches) > 1:
            raise NotionError(f"Multiple '{database_title}' data sources exist under the parent page.")
        return matches[0] if matches else None

    def create_books_database(self, parent_page_id: str, database_title: str) -> Dict[str, Any]:
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": database_title}}],
            "description": [
                {
                    "type": "text",
                    "text": {"content": "Books translated through the local book translation pipeline."},
                }
            ],
            "is_inline": True,
            "icon": {"type": "emoji", "emoji": "📚"},
            "initial_data_source": {"properties": database_schema()},
        }
        return self.request("POST", "/databases", payload)

    def ensure_schema(self, data_source_id: str) -> List[str]:
        source = self.request("GET", f"/data_sources/{data_source_id}")
        existing = source.get("properties") or {}
        missing = {name: value for name, value in database_schema().items() if name not in existing}
        if missing:
            self.request("PATCH", f"/data_sources/{data_source_id}", {"properties": missing})
        return sorted(missing)

    def query_book(self, data_source_id: str, book_id: str, title: str) -> List[Dict[str, Any]]:
        """Upsert by stable ID, with a one-time title fallback for legacy rows."""
        payload = {
            "filter": {"property": "Book ID", "rich_text": {"equals": book_id}},
            "page_size": 100,
        }
        matches = self.request("POST", f"/data_sources/{data_source_id}/query", payload).get("results", [])
        if matches:
            return matches
        payload["filter"] = {"property": "Book", "title": {"equals": title}}
        return self.request("POST", f"/data_sources/{data_source_id}/query", payload).get("results", [])

    def create_book_page(self, data_source_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "icon": {"type": "emoji", "emoji": "📕"},
                "properties": properties,
            },
        )

    def update_page_properties(self, page_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def append_blocks(self, page_id: str, blocks: List[Dict[str, Any]]) -> None:
        if len(blocks) > 100:
            raise ValueError("Notion append calls accept at most 100 blocks")
        self.request("PATCH", f"/blocks/{page_id}/children", {"children": blocks})
        time.sleep(0.35)

    def list_children(self, page_id: str) -> List[Dict[str, Any]]:
        children: List[Dict[str, Any]] = []
        cursor = None
        while True:
            path = f"/blocks/{page_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self.request("GET", path)
            children.extend(data.get("results", []))
            if not data.get("has_more"):
                return children
            cursor = data["next_cursor"]

    def delete_block(self, block_id: str) -> None:
        self.request("DELETE", f"/blocks/{block_id}")
        time.sleep(0.35)


def object_title(obj: Dict[str, Any]) -> str:
    title = obj.get("title") or []
    if title:
        return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in title)
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title" or "title" in prop:
            return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in prop.get("title", []))
    return ""


def rich_text_value(value: Optional[str]) -> Dict[str, Any]:
    return {"rich_text": [] if not value else [{"type": "text", "text": {"content": value[:2000]}}]}


def book_properties(
    metadata: Dict[str, Any], read_status: Optional[str], importing: bool = True
) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "Book": {"title": [{"type": "text", "text": {"content": metadata["book_name"]}}]},
        "Book ID": rich_text_value(metadata["book_id"]),
        "Schema Version": {"number": metadata.get("schema_version", SCHEMA_VERSION)},
        "Author": rich_text_value(metadata.get("author")),
        "Translation Status": {"select": {"name": metadata["translation_status"]}},
        "Import Status": {"select": {"name": "Uploading" if importing else "Complete"}},
        "Genre": rich_text_value(metadata.get("genre")),
        "Audience": rich_text_value(metadata.get("audience")),
        "Source Language": {"select": {"name": metadata["source_language"]}},
        "Translation Language": {"select": {"name": metadata["translation_language"]}},
        "Publication Year": {"number": metadata.get("publication_year")},
        "ISBN": rich_text_value(metadata.get("isbn")),
        "Chapters": {"number": metadata.get("chapter_count")},
        "Chunks": {"number": metadata.get("chunk_count")},
        "Repository": {"url": metadata.get("repository")},
    }
    if metadata.get("completed_at"):
        props["Completed At"] = {"date": {"start": metadata["completed_at"]}}
    if read_status is not None:
        props["Read Status"] = {"select": {"name": read_status}}
    return props


def property_plain_text(page: Dict[str, Any], name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    values = prop.get("rich_text") or prop.get("title") or []
    return "".join(item.get("plain_text") or item.get("text", {}).get("content", "") for item in values)


def property_select(page: Dict[str, Any], name: str) -> Optional[str]:
    value = ((page.get("properties") or {}).get(name) or {}).get("select")
    return value.get("name") if value else None


def property_number(page: Dict[str, Any], name: str) -> Optional[float]:
    return ((page.get("properties") or {}).get(name) or {}).get("number")


def _rich_text_content(values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for value in values:
        annotations = value.get("annotations") or {}
        normalized.append(
            {
                "content": value.get("plain_text") or (value.get("text") or {}).get("content", ""),
                "bold": bool(annotations.get("bold")),
                "italic": bool(annotations.get("italic")),
                "strikethrough": bool(annotations.get("strikethrough")),
                "underline": bool(annotations.get("underline")),
                "code": bool(annotations.get("code")),
            }
        )
    return normalized


def canonical_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an input or Notion response block to stable content semantics."""
    block_type = block.get("type")
    body = block.get(block_type, {}) if block_type else {}
    normalized: Dict[str, Any] = {"type": block_type}
    if block_type in {"paragraph", "heading_1", "heading_2", "heading_3", "callout", "code"}:
        normalized["rich_text"] = _rich_text_content(body.get("rich_text") or [])
    if block_type == "callout":
        icon = body.get("icon") or {}
        normalized["icon"] = {"type": icon.get("type"), "emoji": icon.get("emoji")}
    return normalized


def blocks_hash(blocks: List[Dict[str, Any]]) -> str:
    payload = [canonical_block(block) for block in blocks]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def batch_manifest(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "index": index,
            "block_count": len(blocks[offset : offset + UPLOAD_BATCH_SIZE]),
            "hash": blocks_hash(blocks[offset : offset + UPLOAD_BATCH_SIZE]),
        }
        for index, offset in enumerate(range(0, len(blocks), UPLOAD_BATCH_SIZE))
    ]


def marker_text(book_id: str, batch: Dict[str, Any]) -> str:
    return (
        f"{PIPELINE_MARKER_PREFIX}{book_id}:{batch['index']}:"
        f"{batch['block_count']}:{batch['hash']}"
    )


def marker_block(book_id: str, batch: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [plain_text(marker_text(book_id, batch))], "color": "gray"},
    }


def block_plain_text(block: Dict[str, Any]) -> str:
    body = block.get(block.get("type"), {})
    return "".join(
        item.get("plain_text") or (item.get("text") or {}).get("content", "")
        for item in body.get("rich_text") or []
    )


def find_pipeline_batches(
    children: List[Dict[str, Any]], book_id: str
) -> Dict[int, Dict[str, Any]]:
    prefix = f"{PIPELINE_MARKER_PREFIX}{book_id}:"
    found: Dict[int, Dict[str, Any]] = {}
    for position, block in enumerate(children):
        text = block_plain_text(block)
        if not text.startswith(prefix):
            continue
        parts = text[len(prefix) :].split(":")
        if len(parts) != 3:
            raise NotionError("Malformed pipeline batch marker; refusing an unsafe resume.")
        try:
            index, count = int(parts[0]), int(parts[1])
        except ValueError as error:
            raise NotionError("Malformed pipeline batch marker; refusing an unsafe resume.") from error
        if index in found:
            raise NotionError(f"Duplicate pipeline marker for batch {index}; refusing an unsafe resume.")
        content = children[position + 1 : position + 1 + count]
        found[index] = {
            "marker": block,
            "content": content,
            "count": count,
            "hash": parts[2],
            "complete": len(content) == count,
        }
    return found


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_state(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def ensure_database(
    client: NotionClient,
    parent_title: str,
    parent_page_id: Optional[str],
    database_title: str,
    state_path: Path,
) -> Dict[str, str]:
    parent = client.find_parent_page(parent_title, parent_page_id)
    parent_id = parent["id"]
    source = None
    saved = load_state(state_path)
    if (
        saved
        and saved.get("parent_page_id") == parent_id
        and saved.get("database_title") == database_title
        and saved.get("data_source_id")
    ):
        try:
            candidate = client.request("GET", f"/data_sources/{saved['data_source_id']}")
            database_parent = candidate.get("database_parent") or {}
            if database_parent.get("page_id") == parent_id:
                source = candidate
        except NotionError:
            # The state may refer to a deleted or inaccessible data source; search before creating.
            source = None
    if source is None:
        source = client.find_data_source(parent_id, database_title)
    created = False
    if source:
        data_source_id = source["id"]
        database_id = (source.get("parent") or {}).get("database_id")
    else:
        database = client.create_books_database(parent_id, database_title)
        sources = database.get("data_sources") or []
        if not sources:
            database = client.request("GET", f"/databases/{database['id']}")
            sources = database.get("data_sources") or []
        if len(sources) != 1:
            raise NotionError("The created database did not return exactly one initial data source.")
        database_id = database["id"]
        data_source_id = sources[0]["id"]
        created = True
    client.ensure_schema(data_source_id)
    state = dict(saved or {})
    state.update({
        "parent_page_id": parent_id,
        "database_id": database_id,
        "data_source_id": data_source_id,
        "parent_title": parent_title,
        "database_title": database_title,
        "schema_version": SCHEMA_VERSION,
    })
    save_state(state_path, state)
    state["created"] = created
    return state


def sync_book(
    client: NotionClient,
    data_source_id: str,
    book_path: Path,
    metadata: Dict[str, Any],
    read_status: Optional[str],
    replace_content: bool,
    state_path: Path = DEFAULT_STATE,
) -> Dict[str, Any]:
    markdown = book_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    blocks = translated_book_blocks(markdown, metadata)
    content_digest = blocks_hash(blocks)
    manifests = batch_manifest(blocks)
    matches = client.query_book(data_source_id, metadata["book_id"], metadata["book_name"])
    if len(matches) > 1:
        raise NotionError(f"Multiple rows match Book ID {metadata['book_id']}; refusing to choose one.")

    if matches:
        page = matches[0]
        page_id = page["id"]
        existing_book_id = property_plain_text(page, "Book ID")
        if existing_book_id and existing_book_id != metadata["book_id"]:
            raise NotionError(
                f"Legacy title fallback found '{metadata['book_name']}' with a different Book ID; "
                "refusing to overwrite it."
            )
        old_hash = property_plain_text(page, "Translation Hash")
        client.update_page_properties(page_id, book_properties(metadata, read_status, importing=True))
    else:
        create_status = read_status if read_status is not None else "Not Started"
        page = client.create_book_page(
            data_source_id, book_properties(metadata, create_status, importing=True)
        )
        page_id = page["id"]
        old_hash = ""

    state = load_state(state_path) or {}
    uploads = state.setdefault("uploads", {})
    upload_state = uploads.get(metadata["book_id"]) or {}

    def checkpoint(next_batch: int, status: str) -> None:
        uploads[metadata["book_id"]] = {
            "page_id": page_id,
            "translation_hash": digest,
            "content_hash": content_digest,
            "total_blocks": len(blocks),
            "batches": manifests,
            "next_batch": next_batch,
            "status": status,
            "schema_version": SCHEMA_VERSION,
        }
        save_state(state_path, state)

    def validate_marked(
        children: List[Dict[str, Any]], expected: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        found = find_pipeline_batches(children, metadata["book_id"])
        expected_by_index = {item["index"]: item for item in expected}
        for index, remote in found.items():
            item = expected_by_index.get(index)
            if item is None:
                raise NotionError(f"Unexpected pipeline batch {index}; refusing an unsafe resume.")
            if (
                not remote["complete"]
                or remote["count"] != item["block_count"]
                or remote["hash"] != item["hash"]
                or blocks_hash(remote["content"]) != item["hash"]
            ):
                raise NotionError(
                    f"Remote pipeline batch {index} does not match its checkpoint; "
                    "refusing to overwrite possibly edited content."
                )
        return found

    def validated_legacy_checkpoint() -> List[Dict[str, Any]]:
        """Identify an adopted legacy body from its previously verified local manifest."""
        if (
            upload_state.get("page_id") != page_id
            or upload_state.get("status") != "complete"
            or not upload_state.get("batches")
        ):
            return []
        total = upload_state.get("total_blocks")
        if not isinstance(total, int) or total < 0 or len(children) < total:
            return []
        candidate = children[:total]
        offset = 0
        for item in upload_state["batches"]:
            count = item.get("block_count")
            if not isinstance(count, int):
                return []
            batch = candidate[offset : offset + count]
            if len(batch) != count or blocks_hash(batch) != item.get("hash"):
                return []
            offset += count
        if (
            offset != total
            or blocks_hash(candidate) != upload_state.get("content_hash")
        ):
            return []
        return candidate

    try:
        children = client.list_children(page_id)
        marked = find_pipeline_batches(children, metadata["book_id"])
        # The marker is appended separately before an atomic <=100-block batch. A crash
        # between those calls can leave only the final marker, which is safe to remove.
        orphan_markers = [
            remote
            for remote in marked.values()
            if not remote["complete"]
            and not remote["content"]
            and children
            and children[-1].get("id") == remote["marker"].get("id")
        ]
        if orphan_markers:
            client.delete_block(orphan_markers[0]["marker"]["id"])
            children = client.list_children(page_id)
            marked = find_pipeline_batches(children, metadata["book_id"])

        if old_hash == digest:
            if marked:
                verified = validate_marked(children, manifests)
                if len(verified) != len(manifests):
                    # A stale Complete property must not conceal a partial upload.
                    old_hash = ""
                else:
                    remote_blocks = [
                        block
                        for index in range(len(manifests))
                        for block in verified[index]["content"]
                    ]
                    if len(remote_blocks) != len(blocks) or blocks_hash(remote_blocks) != content_digest:
                        raise NotionError("Remote content readback failed despite a matching translation hash.")
            else:
                # Backward-compatible verification for the one legacy unmarked import.
                if len(children) != len(blocks) or blocks_hash(children) != content_digest:
                    raise NotionError(
                        "Translation Hash matches, but the legacy unmarked page body failed readback. "
                        "No content was changed."
                    )

            if old_hash == digest:
                client.update_page_properties(
                    page_id,
                    {
                        **book_properties(metadata, read_status, importing=False),
                        "Translation Hash": rich_text_value(digest),
                        "Content Hash": rich_text_value(content_digest),
                        "Imported Blocks": {"number": len(blocks)},
                    },
                )
                checked_page = client.request("GET", f"/pages/{page_id}")
                _verify_completion_properties(checked_page, digest, content_digest, len(blocks))
                checkpoint(len(manifests), "complete")
                return {
                    "page": checked_page,
                    "created": False,
                    "uploaded": False,
                    "blocks": len(blocks),
                    "hash": digest,
                }

        # A matching interrupted upload can resume without --replace-content.
        same_interrupted_upload = (
            upload_state.get("page_id") == page_id
            and upload_state.get("translation_hash") == digest
            and upload_state.get("content_hash") == content_digest
        )
        if children and not same_interrupted_upload:
            if not replace_content:
                raise NotionError(
                    "The book row contains different or incomplete content. Re-run with "
                    "--replace-content only when it was produced by this pipeline."
                )
            legacy_content = validated_legacy_checkpoint() if not marked else []
            if not marked and not legacy_content:
                raise NotionError(
                    "This unmarked page has no verified legacy checkpoint, so generated blocks "
                    "cannot be distinguished safely from manual blocks. Refusing --replace-content."
                )
            if marked:
                # Validate each old marked batch against its embedded hash before deletion.
                for index, remote in marked.items():
                    if remote["complete"] and blocks_hash(remote["content"]) == remote["hash"]:
                        continue
                    raise NotionError(
                        f"Pipeline batch {index} was edited or interrupted; refusing unsafe replacement."
                    )
                for remote in sorted(marked.values(), key=lambda item: item["marker"]["id"], reverse=True):
                    for block in remote["content"]:
                        client.delete_block(block["id"])
                    client.delete_block(remote["marker"]["id"])
            else:
                for block in legacy_content:
                    client.delete_block(block["id"])
            children = client.list_children(page_id)
            if find_pipeline_batches(children, metadata["book_id"]):
                raise NotionError("Pipeline content deletion did not pass readback verification.")
            marked = {}
            upload_state = {}

        checkpoint(0, "uploading")
        children = client.list_children(page_id)
        marked = validate_marked(children, manifests) if children else {}
        for manifest in manifests:
            index = manifest["index"]
            if index in marked:
                checkpoint(index + 1, "uploading")
                continue
            offset = index * UPLOAD_BATCH_SIZE
            batch = blocks[offset : offset + UPLOAD_BATCH_SIZE]
            client.append_blocks(page_id, [marker_block(metadata["book_id"], manifest)])
            client.append_blocks(page_id, batch)
            children = client.list_children(page_id)
            marked = validate_marked(children, manifests)
            if index not in marked:
                raise NotionError(f"Batch {index} was not visible during remote readback.")
            checkpoint(index + 1, "uploading")

        children = client.list_children(page_id)
        marked = validate_marked(children, manifests)
        if len(marked) != len(manifests):
            raise NotionError("Remote readback found a partial batch manifest.")
        remote_blocks = [
            block for index in range(len(manifests)) for block in marked[index]["content"]
        ]
        if len(remote_blocks) != len(blocks) or blocks_hash(remote_blocks) != content_digest:
            raise NotionError("Final remote block count/hash verification failed.")

        client.update_page_properties(
            page_id,
            {
                "Translation Hash": rich_text_value(digest),
                "Content Hash": rich_text_value(content_digest),
                "Imported Blocks": {"number": len(blocks)},
                "Schema Version": {"number": SCHEMA_VERSION},
                "Import Status": {"select": {"name": "Complete"}},
            },
        )
        page = client.request("GET", f"/pages/{page_id}")
        _verify_completion_properties(page, digest, content_digest, len(blocks))
        checkpoint(len(manifests), "complete")
    except Exception:
        client.update_page_properties(page_id, {"Import Status": {"select": {"name": "Failed"}}})
        current_next = (uploads.get(metadata["book_id"]) or {}).get("next_batch", 0)
        checkpoint(current_next, "failed")
        raise
    return {"page": page, "created": not matches, "uploaded": True, "blocks": len(blocks), "hash": digest}


def _verify_completion_properties(
    page: Dict[str, Any], translation_hash: str, content_hash: str, block_count: int
) -> None:
    if property_select(page, "Import Status") != "Complete":
        raise NotionError("Remote Import Status did not read back as Complete.")
    if property_plain_text(page, "Translation Hash") != translation_hash:
        raise NotionError("Remote Translation Hash did not pass readback.")
    if property_plain_text(page, "Content Hash") != content_hash:
        raise NotionError("Remote Content Hash did not pass readback.")
    if property_number(page, "Imported Blocks") != block_count:
        raise NotionError("Remote Imported Blocks count did not pass readback.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "setup", "sync"])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--parent-title", default="Book Translation")
    parser.add_argument("--parent-page-id")
    parser.add_argument("--database-title", default="Books")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--book", type=Path, default=DEFAULT_BOOK)
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument(
        "--read-status", choices=["Not Started", "Reading", "Read", "On Hold"], default=None
    )
    parser.add_argument("--replace-content", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    book_path = _rooted(root, args.book)
    state_path = _rooted(root, args.state_file)
    metadata = parse_project_metadata(
        args.brief, args.project, args.book, args.progress, root
    )
    markdown = book_path.read_text(encoding="utf-8")
    blocks = translated_book_blocks(markdown, metadata)
    if args.command == "plan":
        print(json.dumps({"metadata": metadata, "properties": list(database_schema()), "blocks": len(blocks)}, ensure_ascii=False, indent=2))
        return 0

    client = NotionClient(load_token(args.env_file))
    state = ensure_database(
        client, args.parent_title, args.parent_page_id, args.database_title, state_path
    )
    print(
        f"{'Created' if state['created'] else 'Using'} database '{args.database_title}' "
        f"(data source {state['data_source_id']})."
    )
    if args.command == "setup":
        return 0

    result = sync_book(
        client,
        state["data_source_id"],
        book_path,
        metadata,
        args.read_status,
        args.replace_content,
        state_path,
    )
    action = "Created" if result["created"] else "Updated"
    upload = f"uploaded {result['blocks']} blocks" if result["uploaded"] else "content already current"
    print(f"{action} '{metadata['book_name']}': {upload}.")
    print(result["page"].get("url", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

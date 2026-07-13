"""Token-efficient immutable packets and bounded parallel worker helpers."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple, TypeVar


T = TypeVar("T")
R = TypeVar("R")


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextSnapshot:
    snapshot_id: str
    project_brief: str
    style_guide: str
    chapter_memory: str
    pilot_excerpt: str
    prompt_hash: str
    reference_hash: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "project_brief": self.project_brief,
            "style_guide": self.style_guide,
            "chapter_memory": self.chapter_memory,
            "pilot_excerpt": self.pilot_excerpt,
            "prompt_hash": self.prompt_hash,
            "reference_hash": self.reference_hash,
        }


def make_context_snapshot(
    root: Path,
    *,
    prompt_text: str,
    reference_text: str,
    memory_chars: int = 3500,
    pilot_chars: int = 1600,
) -> ContextSnapshot:
    project_brief = (root / "context" / "project_brief.md").read_text(encoding="utf-8")
    metadata_path = root / "context" / "book_metadata.json"
    if metadata_path.exists():
        project_brief += "\n\n# Verified book metadata\n\n" + metadata_path.read_text(encoding="utf-8")
    style_guide = (root / "context" / "style_guide.md").read_text(encoding="utf-8")
    memory_path = root / "context" / "chapter_memory.md"
    chapter_memory = memory_path.read_text(encoding="utf-8")[-memory_chars:] if memory_path.exists() else ""
    pilot_path = root / "output" / "chunks" / "chunk-0001.zh-Hant.md"
    pilot_excerpt = pilot_path.read_text(encoding="utf-8")[:pilot_chars] if pilot_path.exists() else ""
    prompt_hash = content_hash(prompt_text)
    reference_hash = content_hash(reference_text)
    payload = "\0".join(
        [project_brief, style_guide, chapter_memory, pilot_excerpt, prompt_hash, reference_hash]
    )
    return ContextSnapshot(
        snapshot_id=content_hash(payload),
        project_brief=project_brief,
        style_guide=style_guide,
        chapter_memory=chapter_memory,
        pilot_excerpt=pilot_excerpt,
        prompt_hash=prompt_hash,
        reference_hash=reference_hash,
    )


def save_context_snapshot(root: Path, snapshot: ContextSnapshot) -> Path:
    path = root / "build" / "context-snapshots" / f"{snapshot.snapshot_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def neighbor_source_context(chunks: Sequence[Dict[str, Any]], position: int, chars: int = 900) -> Tuple[str, str]:
    previous = chunks[position - 1]["source"][-chars:] if position > 0 else "(Beginning of book.)"
    following = chunks[position + 1]["source"][:chars] if position + 1 < len(chunks) else "(End of book.)"
    return previous, following


def immutable_translation_input(
    snapshot: ContextSnapshot,
    chunks: Sequence[Dict[str, Any]],
    position: int,
    relevant_reference: str,
    neighbor_chars: int = 900,
) -> str:
    chunk = chunks[position]
    previous, following = neighbor_source_context(chunks, position, neighbor_chars)
    pilot = snapshot.pilot_excerpt or "(No approved pilot excerpt yet.)"
    memory = snapshot.chapter_memory or "(No prior continuity memory.)"
    return (
        f"PROJECT BRIEF\n{snapshot.project_brief}\n\n"
        f"STYLE GUIDE\n{snapshot.style_guide}\n\n"
        f"RELEVANT APPROVED REFERENCES\n{relevant_reference}\n\n"
        f"FROZEN CONTINUITY MEMORY\n{memory}\n\n"
        f"APPROVED PILOT EXCERPT\n{pilot}\n\n"
        f"PREVIOUS SOURCE TAIL (context only; do not translate)\n{previous}\n\n"
        f"NEXT SOURCE HEAD (context only; do not translate)\n{following}\n\n"
        f"SOURCE CHUNK {chunk['chunk_id']} (source pages {chunk['page_start']}-{chunk['page_end']})\n"
        f"{chunk['source']}"
    )


def bounded_parallel_map(items: Sequence[T], worker: Callable[[T], R], jobs: int) -> List[R]:
    """Run independent work with a hard concurrency bound and stable result ordering."""
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    if jobs == 1:
        return [worker(item) for item in items]
    results: List[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="book-translate") as pool:
        future_to_index: Dict[Future[R], int] = {
            pool.submit(worker, item): index for index, item in enumerate(items)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


def group_consecutive(items: Sequence[T], size: int) -> List[List[T]]:
    if size < 1:
        raise ValueError("size must be at least 1")
    return [list(items[index : index + size]) for index in range(0, len(items), size)]

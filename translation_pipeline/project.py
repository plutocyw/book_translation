"""Create isolated, reusable per-book workspaces."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict


DEFAULT_PROJECT_CONFIG: Dict[str, Any] = {
    "source_pdf": "input/book.pdf",
    "source_page_start": 1,
    "source_page_end": None,
    "source_language": "English",
    "target_language": "Traditional Chinese",
    "target_locale": "zh-Hant-TW",
    "chunk_target_words": 1800,
    "chunk_max_words": 2300,
    "previous_translation_chars": 1200,
    "source_neighbor_chars": 900,
    "chapter_memory_chars": 3500,
    "worker_jobs": 3,
    "worker_group_chunks": 3,
    "metadata_web_search": True,
    "book_audit_enabled": True,
    "max_output_tokens": {
        "metadata": 3000,
        "terminology": 3000,
        "terminology_consolidate": 7000,
        "translate": 10000,
        "review": 7000,
        "finalize": 9000,
        "adjudicate": 7000,
        "book_audit": 7000,
    },
    "models": {
        "metadata": "gpt-5.6-luna",
        "terminology": "gpt-5.6-luna",
        "terminology_consolidate": "gpt-5.6-sol",
        "translate": "gpt-5.6-terra",
        "review": "gpt-5.6-terra",
        "finalize": "gpt-5.6-terra",
        "adjudicate": "gpt-5.6-sol",
        "book_audit": "gpt-5.6-sol",
    },
    "reasoning_effort": {
        "metadata": "low",
        "terminology": "low",
        "terminology_consolidate": "medium",
        "translate": "low",
        "review": "medium",
        "finalize": "medium",
        "adjudicate": "high",
        "book_audit": "high",
    },
}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "book"


def pdf_metadata(source: Path) -> Dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    raw = reader.metadata or {}
    title = str(raw.get("/Title") or "").strip()
    author = str(raw.get("/Author") or "").strip()
    return {"title": title, "author": author, "pages": len(reader.pages)}


def initialize_project(source: Path, project_dir: Path, template_root: Path, force: bool = False) -> Path:
    source = source.expanduser().resolve()
    project_dir = project_dir.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    config_path = project_dir / "project.json"
    if config_path.exists() and not force:
        raise FileExistsError(f"Project already exists: {config_path}")

    metadata = pdf_metadata(source)
    (project_dir / "input").mkdir(parents=True, exist_ok=True)
    (project_dir / "context").mkdir(parents=True, exist_ok=True)
    (project_dir / "prompts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, project_dir / "input" / "book.pdf")
    for prompt_path in (template_root / "prompts").glob("*.md"):
        shutil.copyfile(prompt_path, project_dir / "prompts" / prompt_path.name)

    config = json.loads(json.dumps(DEFAULT_PROJECT_CONFIG))
    config["source_page_end"] = metadata["pages"]
    config["source_metadata"] = metadata
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    title = metadata["title"] or "TODO: infer from the PDF"
    author = metadata["author"] or "TODO: infer from the PDF"
    (project_dir / "context" / "project_brief.md").write_text(
        "# Project brief\n\n"
        f"- Book: *{title}*\n"
        f"- Author: {author}\n"
        f"- Source edition: PDF supplied by the user; {metadata['pages']} pages\n"
        "- Genre: TODO: infer and verify\n"
        "- Primary audience: Mature Traditional Chinese readers appropriate to the source genre\n"
        "- Locale: Taiwan (`zh-Hant-TW`)\n"
        "- Goal: Faithful, natural, publication-quality translation\n"
        "- Preserve: Meaning, characterization, tone, imagery, structure, and intentional ambiguity\n"
        "- Avoid: Simplified Chinese, abridgment, explanatory additions, and unnatural calques\n",
        encoding="utf-8",
    )
    (project_dir / "context" / "style_guide.md").write_text(
        "# Style guide\n\n"
        "- Use publication-quality Traditional Chinese for Taiwan.\n"
        "- Preserve paragraph boundaries, dialogue, emphasis, headings, and scene breaks.\n"
        "- Prefer established official terminology when verifiable; otherwise record a consistent decision.\n"
        "- Do not translate or expose extraction artifacts, headers, footers, or non-narrative back matter.\n",
        encoding="utf-8",
    )
    (project_dir / "context" / "glossary.csv").write_text(
        "source_term,target_term,category,status,first_chunk,notes\n", encoding="utf-8"
    )
    (project_dir / "context" / "characters.csv").write_text(
        "source_name,target_name,aliases,pronouns_or_gender,role,status,notes\n", encoding="utf-8"
    )
    (project_dir / "context" / "chapter_memory.md").write_text(
        "# Chapter continuity memory\n\n", encoding="utf-8"
    )
    (project_dir / "context" / "progress.md").write_text(
        "# Translation progress\n\n- Status: initialized\n- Completed chunks: 0\n",
        encoding="utf-8",
    )
    return config_path

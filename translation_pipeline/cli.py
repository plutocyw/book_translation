from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path.cwd()
DEFAULT_CONFIG = ROOT / "project.json"

BOILERPLATE_LINES = {
    "ABC Amber LIT Converter",
    "http://www.processtext.com/abclit.html",
}

CHAPTER_HEADINGS = {
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
    "Twenty",
    "Epilogue",
    "About the Author",
    "About The Author",
}

SOURCE_TEXT_REPLACEMENTS = {
    "naÔvetÈ": "naïveté",
    "socalled": "so-called",
    "thisquest": "this quest",
    "showthe": "show the",
}


class PipelineError(RuntimeError):
    pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def sha256_text(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def config_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise PipelineError(f"Config not found: {path}")
    cfg = load_json(path)
    cfg["_config_path"] = str(path)
    return cfg


def project_path(cfg: Dict[str, Any], value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(cfg["_config_path"]).parent / path


def prompt(name: str) -> str:
    return read_text(ROOT / "prompts" / f"{name}.md").strip()


def normalize_page_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
    blocks: List[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in block.splitlines()]
        lines = [line for line in lines if line]
        if lines:
            blocks.append(" ".join(lines))
    return "\n\n".join(blocks).strip()


def clean_extracted_line(raw: str) -> str:
    value = unicodedata.normalize("NFKC", raw or "").replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value).strip()
    for broken, corrected in SOURCE_TEXT_REPLACEMENTS.items():
        value = value.replace(broken, corrected)
    return value


def join_wrapped_lines(lines: Sequence[str]) -> str:
    value = ""
    for raw in lines:
        line = clean_extracted_line(raw)
        if not line:
            continue
        if not value:
            value = line
        elif re.search(r"\w-$", value) and re.match(r"^\w", line):
            value = value[:-1] + line
        else:
            value += " " + line
    return value.strip()


def can_end_paragraph(line: str) -> bool:
    value = line.rstrip()
    return bool(re.search(r"(?:[.!?…—][\"'”’)]*|\. \. \.)$", value))


def page_text_from_positioned_lines(lines: Sequence[Dict[str, Any]]) -> Tuple[str, bool]:
    """Reconstruct paragraphs from PDF lines using indentation and vertical spacing."""
    content: List[Dict[str, Any]] = []
    for item in lines:
        text = clean_extracted_line(str(item.get("text", "")))
        if not text or text in BOILERPLATE_LINES:
            continue
        content.append({"text": text, "x0": float(item.get("x0", 0)), "top": float(item.get("top", 0))})

    if not content:
        return "", False

    first = content[0]
    continues_from_previous = first["text"] not in CHAPTER_HEADINGS and first["x0"] < 92.0
    blocks: List[List[str]] = []
    current: List[str] = []
    previous: Optional[Dict[str, Any]] = None

    for item in content:
        text = item["text"]
        is_heading = text in CHAPTER_HEADINGS
        previous_is_heading = bool(previous and previous["text"] in CHAPTER_HEADINGS)
        vertical_gap = item["top"] - previous["top"] if previous else 0
        begins_paragraph = (
            not current
            or is_heading
            or previous_is_heading
            or (item["x0"] >= 92.0 and can_end_paragraph(current[-1]))
            or (vertical_gap > 20.0 and can_end_paragraph(current[-1]))
        )
        if begins_paragraph and current:
            blocks.append(current)
            current = []
        current.append(text)
        previous = item

    if current:
        blocks.append(current)
    return "\n\n".join(join_wrapped_lines(block) for block in blocks if block), continues_from_previous


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text, flags=re.UNICODE))


def sentence_units(text: str, max_words: int) -> List[str]:
    if word_count(text) <= max_words:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+(?=[\"'“‘A-Z0-9])", text)
    units: List[str] = []
    current: List[str] = []
    count = 0
    for sentence in sentences:
        wc = word_count(sentence)
        if current and count + wc > max_words:
            units.append(" ".join(current))
            current, count = [], 0
        if wc > max_words:
            words = sentence.split()
            for start in range(0, len(words), max_words):
                units.append(" ".join(words[start : start + max_words]))
        else:
            current.append(sentence)
            count += wc
    if current:
        units.append(" ".join(current))
    return [unit for unit in units if unit.strip()]


@dataclass
class Paragraph:
    page_start: int
    page_end: int
    text: str


def make_chunks(pages: Sequence[Dict[str, Any]], target: int, maximum: int) -> List[Dict[str, Any]]:
    paragraphs: List[Paragraph] = []
    for page in pages:
        page_number = int(page["page"])
        blocks = [block.strip() for block in re.split(r"\n\s*\n", page["text"]) if block.strip()]
        if blocks and page.get("continues_from_previous") and paragraphs:
            paragraphs[-1].text = join_wrapped_lines([paragraphs[-1].text, blocks[0]])
            paragraphs[-1].page_end = page_number
            blocks = blocks[1:]
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            paragraphs.append(Paragraph(page_number, page_number, block))

    units: List[Paragraph] = []
    for paragraph in paragraphs:
        for unit in sentence_units(paragraph.text, maximum):
            units.append(Paragraph(paragraph.page_start, paragraph.page_end, unit))
    paragraphs = units

    chunks: List[Dict[str, Any]] = []
    current: List[Paragraph] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if not current:
            return
        source = "\n\n".join(item.text for item in current)
        index = len(chunks) + 1
        chunks.append(
            {
                "chunk_id": f"chunk-{index:04d}",
                "index": index,
                "page_start": current[0].page_start,
                "page_end": current[-1].page_end,
                "source_words": word_count(source),
                "source_chars": len(source),
                "source_sha256": sha256_text(source),
                "source": source,
            }
        )
        current, current_words = [], 0

    for paragraph in paragraphs:
        wc = word_count(paragraph.text)
        if current and (
            paragraph.text in CHAPTER_HEADINGS
            or current_words + wc > maximum
            or current_words >= target
        ):
            flush()
        current.append(paragraph)
        current_words += wc
    flush()
    return chunks


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def append_csv_rows(path: Path, fieldnames: Sequence[str], rows: Iterable[Dict[str, str]]) -> int:
    existing = read_csv_rows(path)
    new_rows = list(rows)
    if not new_rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing + new_rows)
    return len(new_rows)


def relevant_reference(source: str) -> str:
    source_folded = source.casefold()
    lines: List[str] = []
    for row in read_csv_rows(ROOT / "context" / "glossary.csv"):
        term = (row.get("source_term") or "").strip()
        if term and term.casefold() in source_folded:
            lines.append(
                f"- {term} -> {row.get('target_term','')} "
                f"[{row.get('category','')}; {row.get('status','')}] {row.get('notes','')}"
            )
    for row in read_csv_rows(ROOT / "context" / "characters.csv"):
        name = (row.get("source_name") or "").strip()
        aliases = [x.strip() for x in (row.get("aliases") or "").split("|") if x.strip()]
        if name and any(candidate.casefold() in source_folded for candidate in [name] + aliases):
            lines.append(
                f"- {name} -> {row.get('target_name','')} "
                f"[aliases: {row.get('aliases','')}; status: {row.get('status','')}] {row.get('notes','')}"
            )
    return "\n".join(lines) if lines else "(No matching registered terms.)"


def parse_json_response(text: str) -> Dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise PipelineError(f"Model did not return valid JSON: {exc}") from exc
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as nested:
            raise PipelineError(f"Model did not return valid JSON: {nested}") from nested
    if not isinstance(parsed, dict):
        raise PipelineError("Expected a JSON object from the model")
    return parsed


def call_model(cfg: Dict[str, Any], role: str, instructions: str, user_input: str) -> Tuple[str, Dict[str, Any]]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise PipelineError("OPENAI_API_KEY is not set; local extraction and chunking do not require it")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise PipelineError("Install dependencies with: python3 -m pip install -e .") from exc

    model = cfg["models"][role]
    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": user_input,
        "max_output_tokens": int(cfg.get("max_output_tokens", {}).get(role, 8000)),
    }
    effort = cfg.get("reasoning_effort", {}).get(role)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    response = OpenAI().responses.create(**kwargs)
    usage = getattr(response, "usage", None)
    usage_data = usage.model_dump() if hasattr(usage, "model_dump") else {}
    return response.output_text, {"model": model, "role": role, "usage": usage_data}


def selected_chunks(args: argparse.Namespace) -> List[Dict[str, Any]]:
    path = ROOT / "build" / "chunks.jsonl"
    if not path.exists():
        raise PipelineError("No chunks found. Run `book-translate extract` and `book-translate chunk` first.")
    rows = list(iter_jsonl(path))
    start = args.start or 1
    end = args.end or len(rows)
    rows = [row for row in rows if start <= int(row["index"]) <= end]
    if getattr(args, "limit", None):
        rows = rows[: args.limit]
    return rows


def cache_hit(meta_path: Path, input_hash: str, force: bool) -> bool:
    if force or not meta_path.exists():
        return False
    try:
        return load_json(meta_path).get("input_hash") == input_hash
    except (json.JSONDecodeError, OSError):
        return False


def cmd_doctor(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    checks = {
        "python": sys.executable,
        "pdftotext": shutil.which("pdftotext") or "missing",
        "pdftoppm": shutil.which("pdftoppm") or "missing",
        "pdfinfo": shutil.which("pdfinfo") or "missing",
        "ocrmypdf": shutil.which("ocrmypdf") or "optional/missing",
        "tesseract": shutil.which("tesseract") or "optional/missing",
        "OPENAI_API_KEY": "set" if os.environ.get("OPENAI_API_KEY") else "not set (only needed for model calls)",
        "source_pdf": str(project_path(cfg, cfg["source_pdf"])),
    }
    try:
        import pypdf  # noqa: F401

        checks["pypdf"] = "installed"
    except ImportError:
        checks["pypdf"] = "missing"
    try:
        import openai  # noqa: F401

        checks["openai"] = "installed"
    except ImportError:
        checks["openai"] = "missing"
    try:
        import pdfplumber  # noqa: F401

        checks["pdfplumber"] = "installed"
    except ImportError:
        checks["pdfplumber"] = "missing"
    for key, value in checks.items():
        print(f"{key:16} {value}")


def pdf_stats(pdf: Path) -> Dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PipelineError("pypdf is required; run: python3 -m pip install -e .") from exc
    if not pdf.exists():
        raise PipelineError(f"PDF not found: {pdf}")
    reader = PdfReader(str(pdf))
    counts = [len((page.extract_text() or "").strip()) for page in reader.pages]
    low = [index + 1 for index, count in enumerate(counts) if count < 80]
    return {
        "file": str(pdf),
        "pages": len(reader.pages),
        "characters_extracted": sum(counts),
        "median_characters_per_page": sorted(counts)[len(counts) // 2] if counts else 0,
        "low_text_pages": low,
        "likely_scanned": bool(counts) and len(low) / len(counts) > 0.5,
    }


def cmd_inspect(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    pdf = project_path(cfg, cfg["source_pdf"])
    stats = pdf_stats(pdf)
    write_json(ROOT / "build" / "inspection.json", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats["likely_scanned"]:
        print("\nMost pages contain little extractable text. Run the OCR command before extraction.")


def cmd_ocr(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    source = project_path(cfg, cfg["source_pdf"])
    executable = shutil.which("ocrmypdf")
    if not executable:
        raise PipelineError("ocrmypdf is not installed")
    output = ROOT / "build" / "ocr" / "book.ocr.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [executable, "--skip-text", "--deskew", "--rotate-pages", str(source), str(output)]
    print("Running:", " ".join(command))
    subprocess.run(command, check=True)
    print(f"OCR PDF written to {output}. Set source_pdf in project.json to that path before extraction.")


def cmd_extract(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    try:
        import pdfplumber
    except ImportError as exc:
        raise PipelineError("pdfplumber is required; run: python3 -m pip install -e .") from exc
    pdf = project_path(cfg, cfg["source_pdf"])
    if not pdf.exists():
        raise PipelineError(f"PDF not found: {pdf}")
    pages: List[Dict[str, Any]] = []
    with pdfplumber.open(str(pdf)) as document:
        page_start = max(1, int(cfg.get("source_page_start", 1)))
        page_end = min(len(document.pages), int(cfg.get("source_page_end", len(document.pages))))
        for index in range(page_start, page_end + 1):
            page = document.pages[index - 1]
            lines = page.extract_text_lines(layout=False, strip=True, return_chars=False, x_tolerance=1)
            text, continues = page_text_from_positioned_lines(lines)
            if index == page_start:
                continues = False
            pages.append(
                {
                    "page": index,
                    "characters": len(text),
                    "words": word_count(text),
                    "sha256": sha256_text(text),
                    "continues_from_previous": continues,
                    "text": text,
                }
            )
    write_jsonl(ROOT / "build" / "pages.jsonl", pages)
    low = [row["page"] for row in pages if row["characters"] < 80]
    print(f"Extracted {len(pages)} pages and {sum(row['words'] for row in pages):,} words.")
    if low:
        preview = ", ".join(str(x) for x in low[:20])
        suffix = "..." if len(low) > 20 else ""
        print(f"Warning: {len(low)} low-text pages ({preview}{suffix}); inspect for scans, images, or blank pages.")


def cmd_chunk(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    pages_path = ROOT / "build" / "pages.jsonl"
    if not pages_path.exists():
        raise PipelineError("No extracted pages. Run `book-translate extract` first.")
    pages = list(iter_jsonl(pages_path))
    chunks = make_chunks(pages, int(cfg["chunk_target_words"]), int(cfg["chunk_max_words"]))
    write_jsonl(ROOT / "build" / "chunks.jsonl", chunks)
    total = sum(chunk["source_words"] for chunk in chunks)
    print(f"Created {len(chunks)} chunks from {total:,} source words.")
    for chunk in chunks[:5]:
        print(f"  {chunk['chunk_id']}: {chunk['source_words']} words, pages {chunk['page_start']}-{chunk['page_end']}")
    if len(chunks) > 5:
        print("  ...")


def cmd_estimate(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    chunks = selected_chunks(args)
    source_words = sum(int(row["source_words"]) for row in chunks)
    # Deliberately conservative language-independent approximations; actual usage is recorded per call.
    source_tokens = round(source_words * 1.35)
    context_tokens = round(len(chunks) * 900)
    output_tokens = round(source_words * 1.15)
    model = cfg["models"]["translate"]
    pricing = cfg.get("pricing_per_million_tokens_usd", {}).get(model)
    result: Dict[str, Any] = {
        "chunks": len(chunks),
        "source_words": source_words,
        "estimated_input_tokens": source_tokens + context_tokens,
        "estimated_output_tokens": output_tokens,
        "translation_model": model,
        "note": "Approximation only; terminology and review passes are not included.",
    }
    if pricing:
        result["estimated_translation_cost_usd"] = round(
            (result["estimated_input_tokens"] * float(pricing["input"]) + output_tokens * float(pricing["output"]))
            / 1_000_000,
            2,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_terms(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    out_dir = ROOT / "build" / "terms"
    out_dir.mkdir(parents=True, exist_ok=True)
    new_glossary: List[Dict[str, str]] = []
    new_characters: List[Dict[str, str]] = []
    known_terms = {row.get("source_term", "").casefold() for row in read_csv_rows(ROOT / "context" / "glossary.csv")}
    known_characters = {row.get("source_name", "").casefold() for row in read_csv_rows(ROOT / "context" / "characters.csv")}
    instructions = prompt("terminology")
    brief = read_text(ROOT / "context" / "project_brief.md")

    for chunk in selected_chunks(args):
        model = cfg["models"]["terminology"]
        user_input = f"PROJECT BRIEF\n{brief}\n\nSOURCE CHUNK {chunk['chunk_id']}\n{chunk['source']}"
        input_hash = sha256_text(model, instructions, user_input)
        result_path = out_dir / f"{chunk['chunk_id']}.json"
        meta_path = out_dir / f"{chunk['chunk_id']}.meta.json"
        if cache_hit(meta_path, input_hash, args.force):
            print(f"skip {chunk['chunk_id']} (cached)")
            result = load_json(result_path)
        else:
            raw, metadata = call_model(cfg, "terminology", instructions, user_input)
            result = parse_json_response(raw)
            write_json(result_path, result)
            write_json(meta_path, {"input_hash": input_hash, **metadata})
            print(f"scanned {chunk['chunk_id']}")
        for item in result.get("terms", []):
            source_term = str(item.get("source_term", "")).strip()
            target = str(item.get("proposed_target", "")).strip()
            category = str(item.get("category", "other")).strip()
            if not source_term:
                continue
            key = source_term.casefold()
            notes = f"{item.get('notes','')} [confidence: {item.get('confidence','')}]".strip()
            if category == "character":
                if key not in known_characters:
                    new_characters.append(
                        {
                            "source_name": source_term,
                            "target_name": target,
                            "aliases": "",
                            "pronouns_or_gender": "",
                            "role": "",
                            "status": "provisional",
                            "notes": notes,
                        }
                    )
                    known_characters.add(key)
            elif key not in known_terms:
                new_glossary.append(
                    {
                        "source_term": source_term,
                        "target_term": target,
                        "category": category,
                        "status": "provisional",
                        "first_chunk": chunk["chunk_id"],
                        "notes": notes,
                    }
                )
                known_terms.add(key)

    glossary_count = append_csv_rows(
        ROOT / "context" / "glossary.csv",
        ["source_term", "target_term", "category", "status", "first_chunk", "notes"],
        new_glossary,
    )
    character_count = append_csv_rows(
        ROOT / "context" / "characters.csv",
        ["source_name", "target_name", "aliases", "pronouns_or_gender", "role", "status", "notes"],
        new_characters,
    )
    print(f"Added {glossary_count} provisional glossary terms and {character_count} provisional characters.")
    print("Review and approve the CSV entries before bulk translation.")


def previous_translation(index: int, chars: int) -> str:
    if index <= 1:
        return "(Beginning of book.)"
    path = ROOT / "output" / "chunks" / f"chunk-{index - 1:04d}.zh-Hant.md"
    if not path.exists():
        return "(Previous chunk has not been translated.)"
    value = read_text(path).strip()
    return value[-chars:]


def translation_input(cfg: Dict[str, Any], chunk: Dict[str, Any]) -> str:
    memory = read_text(ROOT / "context" / "chapter_memory.md")
    memory = memory[-int(cfg.get("chapter_memory_chars", 3500)) :]
    return textwrap.dedent(
        f"""
        PROJECT BRIEF
        {read_text(ROOT / 'context' / 'project_brief.md')}

        STYLE GUIDE
        {read_text(ROOT / 'context' / 'style_guide.md')}

        RELEVANT APPROVED AND PROVISIONAL REFERENCES
        {relevant_reference(chunk['source'])}

        COMPACT CONTINUITY MEMORY
        {memory}

        END OF PREVIOUS TRANSLATION
        {previous_translation(int(chunk['index']), int(cfg.get('previous_translation_chars', 1200)))}

        SOURCE CHUNK {chunk['chunk_id']} (source pages {chunk['page_start']}-{chunk['page_end']})
        {chunk['source']}
        """
    ).strip()


def cmd_translate(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    out_dir = ROOT / "output" / "chunks"
    out_dir.mkdir(parents=True, exist_ok=True)
    instructions = prompt("translate")
    for chunk in selected_chunks(args):
        user_input = translation_input(cfg, chunk)
        model = cfg["models"]["translate"]
        input_hash = sha256_text(model, instructions, user_input)
        output_path = out_dir / f"{chunk['chunk_id']}.zh-Hant.md"
        meta_path = out_dir / f"{chunk['chunk_id']}.meta.json"
        if output_path.exists() and cache_hit(meta_path, input_hash, args.force):
            print(f"skip {chunk['chunk_id']} (cached)")
            continue
        translated, metadata = call_model(cfg, "translate", instructions, user_input)
        translated = translated.strip() + "\n"
        write_text(output_path, translated)
        write_json(
            meta_path,
            {
                "chunk_id": chunk["chunk_id"],
                "source_sha256": chunk["source_sha256"],
                "translation_sha256": sha256_text(translated),
                "input_hash": input_hash,
                **metadata,
            },
        )
        print(f"translated {chunk['chunk_id']} -> {output_path}")


def cmd_prepare(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    """Write self-contained translation packets for a Codex-driven workflow."""
    out_dir = ROOT / "build" / "packets"
    out_dir.mkdir(parents=True, exist_ok=True)
    for chunk in selected_chunks(args):
        output_path = ROOT / "output" / "chunks" / f"{chunk['chunk_id']}.zh-Hant.md"
        packet = textwrap.dedent(
            f"""
            # Codex translation packet: {chunk['chunk_id']}

            Save the completed translation to `{output_path}`.

            ## Translation instructions

            {prompt('translate')}

            ## Translation context and source

            {translation_input(cfg, chunk)}
            """
        ).strip() + "\n"
        packet_path = out_dir / f"{chunk['chunk_id']}.translation.md"
        write_text(packet_path, packet)
        print(f"prepared {chunk['chunk_id']} -> {packet_path}")


def review_input(chunk: Dict[str, Any], translation: str, review_record: Optional[Dict[str, Any]] = None) -> str:
    extra = ""
    if review_record:
        extra = "\n\nPRIOR REVIEW ISSUES\n" + json.dumps(review_record, ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        STYLE GUIDE
        {read_text(ROOT / 'context' / 'style_guide.md')}

        RELEVANT REFERENCES
        {relevant_reference(chunk['source'])}

        SOURCE
        {chunk['source']}

        TRANSLATION
        {translation}
        {extra}
        """
    ).strip()


def cmd_review(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    out_dir = ROOT / "output" / "reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    for chunk in selected_chunks(args):
        translation_path = ROOT / "output" / "chunks" / f"{chunk['chunk_id']}.zh-Hant.md"
        if not translation_path.exists():
            print(f"skip {chunk['chunk_id']} (not translated)")
            continue
        translation = read_text(translation_path)
        instructions = prompt("review")
        user_input = review_input(chunk, translation)
        model = cfg["models"]["review"]
        input_hash = sha256_text(model, instructions, user_input)
        result_path = out_dir / f"{chunk['chunk_id']}.review.json"
        meta_path = out_dir / f"{chunk['chunk_id']}.review.meta.json"
        if cache_hit(meta_path, input_hash, args.force):
            print(f"skip {chunk['chunk_id']} review (cached)")
            result = load_json(result_path)
        else:
            raw, metadata = call_model(cfg, "review", instructions, user_input)
            result = parse_json_response(raw)
            write_json(result_path, result)
            write_json(meta_path, {"input_hash": input_hash, **metadata})
            print(f"reviewed {chunk['chunk_id']}: {result.get('verdict', 'unknown')}")

        final_result = result
        if result.get("verdict") == "escalate" and args.escalate:
            adj_instructions = prompt("adjudicate")
            adj_input = review_input(chunk, translation, result)
            adj_model = cfg["models"]["adjudicate"]
            adj_hash = sha256_text(adj_model, adj_instructions, adj_input)
            adj_path = out_dir / f"{chunk['chunk_id']}.adjudication.json"
            adj_meta = out_dir / f"{chunk['chunk_id']}.adjudication.meta.json"
            if cache_hit(adj_meta, adj_hash, args.force):
                final_result = load_json(adj_path)
            else:
                raw, metadata = call_model(cfg, "adjudicate", adj_instructions, adj_input)
                final_result = parse_json_response(raw)
                write_json(adj_path, final_result)
                write_json(adj_meta, {"input_hash": adj_hash, **metadata})
            print(f"adjudicated {chunk['chunk_id']}: {final_result.get('verdict', 'unknown')}")

        corrected = final_result.get("corrected_translation")
        if corrected:
            reviewed_path = ROOT / "output" / "chunks" / f"{chunk['chunk_id']}.reviewed.zh-Hant.md"
            write_text(reviewed_path, str(corrected).strip() + "\n")
            if args.apply:
                shutil.copyfile(reviewed_path, translation_path)
                print(f"applied reviewed translation for {chunk['chunk_id']}")


def chosen_translation(chunk_id: str) -> Optional[Path]:
    reviewed = ROOT / "output" / "chunks" / f"{chunk_id}.reviewed.zh-Hant.md"
    normal = ROOT / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
    return reviewed if reviewed.exists() else normal if normal.exists() else None


def cmd_assemble(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    chunks = list(iter_jsonl(ROOT / "build" / "chunks.jsonl"))
    pieces: List[str] = []
    missing: List[str] = []
    for chunk in chunks:
        path = chosen_translation(chunk["chunk_id"])
        if not path:
            missing.append(chunk["chunk_id"])
            continue
        marker = f"<!-- {chunk['chunk_id']}; source pages {chunk['page_start']}-{chunk['page_end']} -->"
        pieces.append(marker + "\n\n" + read_text(path).strip())
    if missing and not args.allow_missing:
        raise PipelineError(f"Missing {len(missing)} translations; first missing: {missing[0]}")
    output = ROOT / "output" / "book.zh-Hant.md"
    write_text(output, "\n\n".join(pieces) + "\n")
    print(f"Assembled {len(pieces)} chunks into {output}")
    if missing:
        print(f"Warning: omitted {len(missing)} missing chunks.")


def cmd_qa(args: argparse.Namespace, cfg: Dict[str, Any]) -> None:
    chunks = selected_chunks(args)
    issues: List[Dict[str, Any]] = []
    target_forms: Dict[str, set] = {}
    for row in read_csv_rows(ROOT / "context" / "glossary.csv"):
        source_term, target_term = row.get("source_term", ""), row.get("target_term", "")
        if source_term and target_term:
            target_forms.setdefault(source_term, set()).add(target_term)
    duplicate_sources = {source: sorted(forms) for source, forms in target_forms.items() if len(forms) > 1}
    if duplicate_sources:
        issues.append({"type": "glossary_conflict", "entries": duplicate_sources})

    for chunk in chunks:
        path = chosen_translation(chunk["chunk_id"])
        if not path:
            issues.append({"chunk_id": chunk["chunk_id"], "type": "missing_translation"})
            continue
        target = read_text(path)
        han_count = len(re.findall(r"[\u3400-\u9fff]", target))
        ratio = han_count / max(1, int(chunk["source_words"]))
        if ratio < 0.35 or ratio > 2.5:
            issues.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "type": "length_ratio",
                    "han_characters_per_source_word": round(ratio, 3),
                }
            )
        markers = [marker for marker in ["TODO", "???", "[[", "TRANSLATION:"] if marker in target]
        if markers:
            issues.append({"chunk_id": chunk["chunk_id"], "type": "placeholder", "markers": markers})
        for row in read_csv_rows(ROOT / "context" / "glossary.csv"):
            source_term = row.get("source_term", "")
            target_term = row.get("target_term", "")
            if (
                row.get("status") == "approved"
                and source_term
                and target_term
                and source_term.casefold() in chunk["source"].casefold()
                and target_term not in target
            ):
                issues.append(
                    {
                        "chunk_id": chunk["chunk_id"],
                        "type": "approved_term_missing",
                        "source_term": source_term,
                        "expected_target": target_term,
                    }
                )
    report = {"chunks": len(chunks), "issue_count": len(issues), "issues": issues}
    write_json(ROOT / "output" / "qa-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if issues:
        raise PipelineError(f"QA found {len(issues)} issue(s); see output/qa-report.json")


def add_range_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=int, help="first 1-based chunk index")
    parser.add_argument("--end", type=int, help="last 1-based chunk index")
    parser.add_argument("--limit", type=int, help="maximum chunks to process")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable PDF book translation pipeline")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="project JSON config")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check local tools and credentials")
    sub.add_parser("inspect", help="inspect PDF text availability")
    sub.add_parser("ocr", help="OCR the source PDF with OCRmyPDF")
    sub.add_parser("extract", help="extract normalized page text")
    sub.add_parser("chunk", help="create paragraph-aware translation chunks")

    estimate = sub.add_parser("estimate", help="estimate translation tokens and cost")
    add_range_args(estimate)

    terms = sub.add_parser("terms", help="discover provisional names and terminology")
    add_range_args(terms)
    terms.add_argument("--force", action="store_true", help="ignore matching cached model output")

    translate = sub.add_parser("translate", help="translate chunks")
    add_range_args(translate)
    translate.add_argument("--force", action="store_true", help="ignore matching cached model output")

    prepare = sub.add_parser("prepare", help="prepare translation packets for Codex without API calls")
    add_range_args(prepare)

    review = sub.add_parser("review", help="bilingual review of translated chunks")
    add_range_args(review)
    review.add_argument("--force", action="store_true", help="ignore matching cached model output")
    review.add_argument("--escalate", action="store_true", help="send only escalated cases to the adjudication model")
    review.add_argument("--apply", action="store_true", help="apply complete corrected translations after saving them separately")

    assemble = sub.add_parser("assemble", help="assemble translated chunks into Markdown")
    assemble.add_argument("--allow-missing", action="store_true")
    qa = sub.add_parser("qa", help="run deterministic completeness and terminology checks")
    add_range_args(qa)
    return parser


COMMANDS = {
    "doctor": cmd_doctor,
    "inspect": cmd_inspect,
    "ocr": cmd_ocr,
    "extract": cmd_extract,
    "chunk": cmd_chunk,
    "estimate": cmd_estimate,
    "terms": cmd_terms,
    "translate": cmd_translate,
    "prepare": cmd_prepare,
    "review": cmd_review,
    "assemble": cmd_assemble,
    "qa": cmd_qa,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(config_path(args.config))
        COMMANDS[args.command](args, cfg)
        return 0
    except (PipelineError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

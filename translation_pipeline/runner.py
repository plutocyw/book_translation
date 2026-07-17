"""End-to-end orchestration for resumable API and Codex translation runs."""

from __future__ import annotations

import csv
import json
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .orchestrator import RunStore, TaskLease, TaskSpec, create_run, open_run, sha256_file, sha256_json


RUNS_DIR = Path(".book-translate/runs")
CURRENT_RUN = Path(".book-translate/current-run")


@dataclass(frozen=True)
class TaskResult:
    output_hash: str
    usage: Mapping[str, Any]
    model: Optional[str] = None


def _role_config(cfg: Mapping[str, Any], role: str, fallback: Optional[str] = None) -> str:
    models = cfg.get("models", {})
    value = models.get(role) or (models.get(fallback) if fallback else None)
    if not value:
        raise ValueError(f"No model configured for role '{role}'")
    return str(value)


def _prompts(root: Path) -> Dict[str, Path]:
    return {path.stem: path for path in sorted((root / "prompts").glob("*.md"))}


def _references(root: Path) -> Dict[str, Path]:
    names = ("project_brief.md", "style_guide.md", "glossary.csv", "characters.csv")
    return {Path(name).stem: root / "context" / name for name in names if (root / "context" / name).exists()}


def current_run(root: Path, run_id: Optional[str] = None) -> RunStore:
    runs = root / RUNS_DIR
    if run_id:
        return open_run(runs, run_id)
    pointer = root / CURRENT_RUN
    if not pointer.exists():
        raise FileNotFoundError("No current run. Start one with `book-translate run`.")
    return open_run(runs, pointer.read_text(encoding="utf-8").strip())


def _write_current_run(root: Path, run_id: str) -> None:
    pointer = root / CURRENT_RUN
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_suffix(".tmp")
    temporary.write_text(run_id + "\n", encoding="utf-8")
    temporary.replace(pointer)


def prepare_sources(root: Path, cfg: Dict[str, Any], force: bool = False) -> None:
    """Run deterministic extraction/chunking before freezing the task graph."""

    from . import cli

    cli.ROOT = root
    empty = type("Args", (), {})()
    if force or not (root / "build" / "inspection.json").exists():
        cli.cmd_inspect(empty, cfg)
    if force or not (root / "build" / "pages.jsonl").exists():
        cli.cmd_extract(empty, cfg)
    if force or not (root / "build" / "chunks.jsonl").exists():
        cli.cmd_chunk(empty, cfg)


def create_pipeline_run(
    root: Path,
    cfg: Dict[str, Any],
    *,
    engine: str,
    jobs: int,
    run_id: Optional[str] = None,
    notion: bool = False,
) -> RunStore:
    """Freeze inputs and create the complete per-book dependency graph."""

    from . import cli

    chunks = list(cli.iter_jsonl(root / "build" / "chunks.jsonl"))
    if not chunks:
        raise ValueError("Extraction produced no translation chunks")
    config_path = Path(cfg["_config_path"])
    run = create_run(
        root / RUNS_DIR,
        source=cli.configured_source(cfg)[0],
        config=config_path,
        prompts=_prompts(root),
        references=_references(root),
        engine=engine,
        concurrency=jobs,
        run_id=run_id,
    )
    specs: List[TaskSpec] = [
        TaskSpec("metadata", "metadata", model=_role_config(cfg, "metadata", "terminology"), priority=100)
    ]
    term_ids: List[str] = []
    for chunk in chunks:
        task_id = f"terms:{chunk['chunk_id']}"
        term_ids.append(task_id)
        specs.append(
            TaskSpec(
                task_id,
                "terminology",
                {"chunk_id": chunk["chunk_id"], "index": chunk["index"]},
                dependencies=("metadata",),
                sequence=int(chunk["index"]),
                priority=80,
                model=_role_config(cfg, "terminology"),
            )
        )
    specs.append(
        TaskSpec(
            "terms:consolidate",
            "terminology_consolidate",
            dependencies=tuple(term_ids),
            sequence=len(chunks) + 1,
            priority=70,
            model=_role_config(cfg, "terminology_consolidate", "adjudicate"),
        )
    )
    review_ids: List[str] = []
    final_ids: List[str] = []
    previous_final: Optional[str] = None
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        index = int(chunk["index"])
        translate_id = f"translate:{chunk_id}"
        review_id = f"review:{chunk_id}"
        final_id = f"finalize:{chunk_id}"
        review_ids.append(review_id)
        final_ids.append(final_id)
        specs.append(
            TaskSpec(
                translate_id,
                "translate",
                {"chunk_id": chunk_id, "index": index},
                dependencies=("terms:consolidate",),
                sequence=index,
                priority=60,
                model=_role_config(cfg, "translate"),
            )
        )
        specs.append(
            TaskSpec(
                review_id,
                "review",
                {"chunk_id": chunk_id, "index": index},
                dependencies=(translate_id,),
                sequence=index,
                priority=50,
                model=_role_config(cfg, "review"),
            )
        )
        dependencies = [review_id]
        if previous_final:
            dependencies.append(previous_final)
        specs.append(
            TaskSpec(
                final_id,
                "finalize",
                {"chunk_id": chunk_id, "index": index},
                dependencies=tuple(dependencies),
                sequence=index,
                priority=40,
                model=_role_config(cfg, "finalize", "review"),
            )
        )
        previous_final = final_id
    specs.append(TaskSpec("assemble", "assemble", dependencies=tuple(final_ids), priority=30))
    specs.append(TaskSpec("quality", "quality", dependencies=("assemble",), priority=20))
    completion_gate = "quality"
    if cfg.get("book_audit_enabled", True):
        specs.append(
            TaskSpec(
                "book-audit",
                "book_audit",
                dependencies=("quality",),
                priority=15,
                model=_role_config(cfg, "book_audit", "adjudicate"),
            )
        )
        completion_gate = "book-audit"
    if notion:
        specs.append(TaskSpec("notion", "notion", dependencies=(completion_gate,), priority=10))
    run.add_tasks(specs)
    _write_current_run(root, run.run_id)
    return run


def _chunk(root: Path, chunk_id: str) -> Dict[str, Any]:
    from . import cli

    for row in cli.iter_jsonl(root / "build" / "chunks.jsonl"):
        if row["chunk_id"] == chunk_id:
            return row
    raise KeyError(chunk_id)


def _usage(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    usage = metadata.get("usage") or {}
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, Mapping):
        return {}
    result: Dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            result[key] = value
    return result


def consolidated_term_candidates(root: Path) -> List[Dict[str, Any]]:
    """Deduplicate per-chunk term scans before the single global consolidation pass."""

    aggregate: Dict[str, Dict[str, Any]] = {}
    for path in sorted((root / "build" / "terms").glob("chunk-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        for item in value.get("terms", []):
            source = str(item.get("source_term", "")).strip()
            if not source:
                continue
            key = source.replace("’", "'").casefold()
            entry = aggregate.setdefault(
                key,
                {
                    "source_forms": {},
                    "candidate_targets": {},
                    "categories": {},
                    "confidence": {},
                    "notes": [],
                    "chunks": [],
                },
            )
            entry["source_forms"][source] = entry["source_forms"].get(source, 0) + 1
            target = str(item.get("proposed_target", "")).strip()
            if target:
                entry["candidate_targets"][target] = entry["candidate_targets"].get(target, 0) + 1
            category = str(item.get("category", "other"))
            entry["categories"][category] = entry["categories"].get(category, 0) + 1
            confidence = str(item.get("confidence", ""))
            if confidence:
                entry["confidence"][confidence] = entry["confidence"].get(confidence, 0) + 1
            note = str(item.get("notes", "")).strip()
            if note and note not in entry["notes"] and len(entry["notes"]) < 3:
                entry["notes"].append(note)
            entry["chunks"].append(path.stem)
    result = []
    for entry in aggregate.values():
        source = max(entry["source_forms"], key=entry["source_forms"].get)
        result.append(
            {
                "source_term": source,
                "mentions": sum(entry["source_forms"].values()),
                "source_forms": entry["source_forms"],
                "candidate_targets": entry["candidate_targets"],
                "categories": entry["categories"],
                "confidence": entry["confidence"],
                "notes": entry["notes"],
                "first_chunk": min(entry["chunks"]),
            }
        )
    return sorted(result, key=lambda item: (-item["mentions"], item["source_term"].casefold()))


def _write_approved_references(root: Path, result: Mapping[str, Any]) -> None:
    """Persist generated terminology without mutating the immutable user registries."""

    path = root / "context" / "approved_terminology.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def execute_task(root: Path, cfg: Dict[str, Any], lease: TaskLease) -> TaskResult:
    """Execute one API task. File writes are content-addressed by the queue metadata."""

    from . import cli

    cli.ROOT = root
    stage = lease.stage
    model = lease.model
    if stage == "metadata":
        chunks = list(cli.iter_jsonl(root / "build" / "chunks.jsonl"))
        excerpt = "\n\n".join(row["source"] for row in chunks[:3])[:18000]
        raw, metadata = cli.call_model(
            cfg,
            "metadata" if "metadata" in cfg.get("models", {}) else "terminology",
            cli.prompt("metadata"),
            "PDF METADATA\n" + json.dumps(cfg.get("source_metadata", {}), ensure_ascii=False) + "\n\nEXCERPT\n" + excerpt,
            tools=[{"type": "web_search"}] if cfg.get("metadata_web_search", True) else None,
        )
        result = cli.parse_json_response(raw)
        output = root / "context" / "book_metadata.json"
        cli.write_json(output, result)
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "terminology":
        chunk = _chunk(root, str(lease.payload["chunk_id"]))
        raw, metadata = cli.call_model(
            cfg,
            "terminology",
            cli.prompt("terminology"),
            f"PROJECT BRIEF\n{cli.read_text(root / 'context' / 'project_brief.md')}\n\nSOURCE\n{chunk['source']}",
        )
        result = cli.parse_json_response(raw)
        output = root / "build" / "terms" / f"{chunk['chunk_id']}.json"
        cli.write_json(output, result)
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "terminology_consolidate":
        proposals = consolidated_term_candidates(root)
        role = "terminology_consolidate" if "terminology_consolidate" in cfg.get("models", {}) else "adjudicate"
        raw, metadata = cli.call_model(
            cfg,
            role,
            cli.prompt("terminology_consolidate"),
            f"PROJECT BRIEF\n{cli.read_text(root / 'context' / 'project_brief.md')}\n\n"
            f"EXISTING APPROVED REFERENCES\n{cli.approved_reference_snapshot()}\n\n"
            f"PROPOSALS\n{json.dumps(proposals, ensure_ascii=False)}",
        )
        result = cli.parse_json_response(raw)
        _write_approved_references(root, result)
        output = root / "context" / "approved_terminology.json"
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "translate":
        chunk = _chunk(root, str(lease.payload["chunk_id"]))
        instructions = cli.prompt("translate")
        user_input = cli.translation_input(cfg, chunk)
        translated, metadata = cli.call_model(cfg, "translate", instructions, user_input)
        output = root / "output" / "chunks" / f"{chunk['chunk_id']}.zh-Hant.md"
        cli.write_text(output, translated.strip() + "\n")
        cli.write_json(
            output.with_name(f"{chunk['chunk_id']}.meta.json"),
            {"chunk_id": chunk["chunk_id"], "source_sha256": chunk["source_sha256"], "translation_sha256": sha256_file(output), **metadata},
        )
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "review":
        chunk = _chunk(root, str(lease.payload["chunk_id"]))
        translation_path = root / "output" / "chunks" / f"{chunk['chunk_id']}.zh-Hant.md"
        translation = cli.read_text(translation_path)
        raw, metadata = cli.call_model(cfg, "review", cli.prompt("review"), cli.review_input(chunk, translation))
        result = cli.parse_json_response(raw)
        if result.get("verdict") == "escalate":
            adjudicated, adjudication_metadata = cli.call_model(
                cfg,
                "adjudicate",
                cli.prompt("adjudicate"),
                cli.review_input(chunk, translation, result),
            )
            result = cli.parse_json_response(adjudicated)
            metadata = adjudication_metadata
        result.update({"source_sha256": chunk["source_sha256"], "translation_sha256": sha256_file(translation_path)})
        output = root / "output" / "reviews" / f"{chunk['chunk_id']}.review.json"
        cli.write_json(output, result)
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "finalize":
        chunk = _chunk(root, str(lease.payload["chunk_id"]))
        chunk_id = chunk["chunk_id"]
        draft = cli.read_text(root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md")
        review = cli.load_json(root / "output" / "reviews" / f"{chunk_id}.review.json")
        candidate = str(review.get("corrected_translation") or draft).strip()
        previous = "(Beginning of book.)"
        if int(chunk["index"]) > 1:
            previous_path = root / "output" / "chunks" / f"chunk-{int(chunk['index']) - 1:04d}.final.zh-Hant.md"
            if previous_path.exists():
                previous = cli.read_text(previous_path)[-int(cfg.get("previous_translation_chars", 1200)):]
        user_input = (
            f"STYLE GUIDE\n{cli.read_text(root / 'context' / 'style_guide.md')}\n\n"
            f"APPROVED REFERENCES\n{cli.relevant_reference(chunk['source'])}\n\n"
            f"PREVIOUS FINALIZED TARGET TAIL\n{previous}\n\nSOURCE\n{chunk['source']}\n\n"
            f"BILINGUAL REVIEW\n{json.dumps(review, ensure_ascii=False)}\n\nREVIEWED CANDIDATE\n{candidate}"
        )
        role = "finalize" if "finalize" in cfg.get("models", {}) else "review"
        final, metadata = cli.call_model(cfg, role, cli.prompt("finalize"), user_input)
        output = root / "output" / "chunks" / f"{chunk_id}.final.zh-Hant.md"
        cli.write_text(output, final.strip() + "\n")
        cli.write_json(
            output.with_name(f"{chunk_id}.final.meta.json"),
            {"chunk_id": chunk_id, "source_sha256": chunk["source_sha256"], "draft_sha256": sha256_file(root / 'output' / 'chunks' / f'{chunk_id}.zh-Hant.md'), "review_sha256": sha256_file(root / 'output' / 'reviews' / f'{chunk_id}.review.json'), "translation_sha256": sha256_file(output), **metadata},
        )
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "quality":
        from .quality import run_quality_gate

        report = run_quality_gate(root, cli.merged_quality_config(cfg))
        output = root / "output" / "qa-report.json"
        cli.write_json(output, report.to_dict())
        report.raise_for_errors()
        progress = root / "context" / "progress.md"
        completed = len(list(cli.iter_jsonl(root / "build" / "chunks.jsonl")))
        cli.write_text(
            progress,
            f"# Translation progress\n\n- Status: complete\n- Completed chunks: {completed}\n- Completed at: {time.strftime('%Y-%m-%d')}\n",
        )
        return TaskResult(sha256_file(output), {}, None)

    if stage == "assemble":
        args = type("Args", (), {"allow_missing": False})()
        cli.cmd_assemble(args, cfg)
        output = root / "output" / "book.zh-Hant.md"
        return TaskResult(sha256_file(output), {}, None)

    if stage == "book_audit":
        chunks = list(cli.iter_jsonl(root / "build" / "chunks.jsonl"))
        transitions: List[Dict[str, str]] = []
        for index, chunk in enumerate(chunks):
            first = str(chunk["source"]).split("\n\n", 1)[0].strip()
            if index == 0 or cli.is_chapter_heading(first):
                target = cli.chosen_translation(str(chunk["chunk_id"]))
                previous_target = cli.chosen_translation(str(chunks[index - 1]["chunk_id"])) if index else None
                transitions.append(
                    {
                        "chunk_id": str(chunk["chunk_id"]),
                        "previous_tail": cli.read_text(previous_target)[-600:] if previous_target else "(Beginning of book.)",
                        "current_head": cli.read_text(target)[:900] if target else "",
                    }
                )
        review_issues = []
        for path in sorted((root / "output" / "reviews").glob("chunk-*.review.json")):
            value = cli.load_json(path)
            if value.get("issues"):
                review_issues.append({"chunk_id": path.name.split(".review", 1)[0], "issues": value["issues"]})
        role = "book_audit" if "book_audit" in cfg.get("models", {}) else "adjudicate"
        raw, metadata = cli.call_model(
            cfg,
            role,
            cli.prompt("book_audit"),
            f"APPROVED REFERENCES\n{cli.approved_reference_snapshot()}\n\n"
            f"CHAPTER TRANSITIONS\n{json.dumps(transitions, ensure_ascii=False)}\n\n"
            f"REVIEW ISSUES ALREADY PROCESSED BY FINALIZER\n{json.dumps(review_issues, ensure_ascii=False)}",
        )
        result = cli.parse_json_response(raw)
        output = root / "output" / "book-audit.json"
        cli.write_json(output, result)
        if result.get("verdict") != "pass" or result.get("issues"):
            raise RuntimeError("Book audit requires revision; see output/book-audit.json")
        return TaskResult(sha256_file(output), _usage(metadata), model)

    if stage == "notion":
        from . import notion_sync

        notion = cfg.get("notion", {})
        argv = ["sync", "--root", str(root), "--parent-title", str(notion.get("parent_title", "Book Translation"))]
        if notion.get("env_file"):
            argv += ["--env-file", str(notion["env_file"])]
        if notion.get("read_status"):
            argv += ["--read-status", str(notion["read_status"])]
        result = notion_sync.main(argv)
        if result:
            raise RuntimeError(f"Notion sync failed with exit code {result}")
        state = root / ".notion-state.json"
        return TaskResult(sha256_file(state), {}, None)

    raise ValueError(f"Unsupported task stage: {stage}")


def execute_api_run(root: Path, cfg: Dict[str, Any], run: RunStore) -> Dict[str, Any]:
    """Drain a run using a fixed-width worker pool and durable task leases."""

    jobs = int(run.manifest["concurrency"])
    in_flight: Dict[Future[TaskResult], TaskLease] = {}
    worker_counter = 0
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="book-pipeline") as pool:
        while True:
            while len(in_flight) < jobs:
                worker_counter += 1
                worker_id = f"api-{threading.get_ident()}-{worker_counter}"
                lease = run.claim(worker_id, lease_seconds=1800)
                if lease is None:
                    break
                in_flight[pool.submit(execute_task, root, cfg, lease)] = lease
            if not in_flight:
                status = run.status()
                if status["by_state"]["ready"] == 0 and status["by_state"]["leased"] == 0:
                    return status
                time.sleep(0.05)
                continue
            done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                lease = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    state = run.fail(lease.task_id, lease.worker_id, str(exc), retryable=True, model=lease.model)
                    if state == "retryable_failed":
                        run.retry(lease.task_id)
                else:
                    run.succeed(lease.task_id, lease.worker_id, result.output_hash, result.usage, result.model)


def codex_ready_packets(root: Path, run: RunStore) -> Path:
    """Write compact descriptors for ready tasks that Codex may delegate in parallel."""

    packet_dir = root / "build" / "packets" / run.run_id
    packet_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for task in run.tasks("ready"):
        row = {
            "task_id": task["task_id"],
            "stage": task["stage"],
            "payload": task["payload"],
            "input_hash": task["input_hash"],
            "model_role": task["model"],
        }
        rows.append(row)
        (packet_dir / f"{task['task_id'].replace(':', '--')}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    manifest = packet_dir / "ready.json"
    manifest.write_text(json.dumps({"run_id": run.run_id, "tasks": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def expected_output(root: Path, task: Mapping[str, Any]) -> Path:
    """Return the canonical artifact path for a queued task."""

    chunk_id = str((task.get("payload") or {}).get("chunk_id", ""))
    stage = str(task["stage"])
    paths = {
        "metadata": root / "context" / "book_metadata.json",
        "terminology": root / "build" / "terms" / f"{chunk_id}.json",
        "terminology_consolidate": root / "context" / "approved_terminology.json",
        "translate": root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md",
        "review": root / "output" / "reviews" / f"{chunk_id}.review.json",
        "finalize": root / "output" / "chunks" / f"{chunk_id}.final.zh-Hant.md",
        "assemble": root / "output" / "book.zh-Hant.md",
        "quality": root / "output" / "qa-report.json",
        "book_audit": root / "output" / "book-audit.json",
        "notion": root / ".notion-state.json",
    }
    if stage not in paths:
        raise ValueError(f"Unsupported task stage: {stage}")
    return paths[stage]


def _translation_snapshot(root: Path, run: RunStore):
    from . import cli
    from .workers import ContextSnapshot, make_context_snapshot

    path = run.path / "translation-context.json"
    if path.exists():
        return ContextSnapshot(**json.loads(path.read_text(encoding="utf-8")))
    snapshot = make_context_snapshot(
        root,
        prompt_text=cli.prompt("translate"),
        reference_text=cli.approved_reference_snapshot(),
        memory_chars=3500,
        pilot_chars=0,
    )
    path.write_text(json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def _codex_packet(root: Path, task: Mapping[str, Any], output: Path, run: RunStore) -> str:
    """Materialize the exact, bounded context a Codex worker should consume."""

    from . import cli

    cli.ROOT = root
    stage = str(task["stage"])
    chunk_id = str((task.get("payload") or {}).get("chunk_id", ""))
    prompt_path = root / "prompts" / f"{stage}.md"
    instructions = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    body = ""
    if stage == "metadata":
        chunks = list(cli.iter_jsonl(root / "build" / "chunks.jsonl"))
        body = "PDF CONFIG\n" + (root / "project.json").read_text(encoding="utf-8")
        body += "\n\nBOOK EXCERPT\n" + "\n\n".join(row["source"] for row in chunks[:3])[:18000]
    elif stage == "terminology":
        chunk = _chunk(root, chunk_id)
        body = f"PROJECT BRIEF\n{cli.read_text(root / 'context' / 'project_brief.md')}\n\nSOURCE\n{chunk['source']}"
    elif stage == "terminology_consolidate":
        proposals = consolidated_term_candidates(root)
        body = f"EXISTING APPROVED REFERENCES\n{cli.approved_reference_snapshot()}\n\nPROPOSALS\n{json.dumps(proposals, ensure_ascii=False)}"
    elif stage == "translate":
        from .workers import immutable_translation_input

        cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
        chunks = list(cli.iter_jsonl(root / "build" / "chunks.jsonl"))
        position = next(index for index, row in enumerate(chunks) if row["chunk_id"] == chunk_id)
        snapshot = _translation_snapshot(root, run)
        body = immutable_translation_input(
            snapshot,
            chunks,
            position,
            cli.relevant_reference(chunks[position]["source"]),
            neighbor_chars=int(cfg.get("source_neighbor_chars", 900)),
        )
    elif stage == "review":
        chunk = _chunk(root, chunk_id)
        body = cli.review_input(chunk, cli.read_text(root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"))
    elif stage == "finalize":
        chunk = _chunk(root, chunk_id)
        draft = cli.read_text(root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md")
        review = cli.load_json(root / "output" / "reviews" / f"{chunk_id}.review.json")
        candidate = str(review.get("corrected_translation") or draft).strip()
        previous = "(Beginning of book.)"
        if int(chunk["index"]) > 1:
            prior = root / "output" / "chunks" / f"chunk-{int(chunk['index']) - 1:04d}.final.zh-Hant.md"
            if prior.exists():
                previous = cli.read_text(prior)[-1200:]
        body = (
            f"STYLE GUIDE\n{cli.read_text(root / 'context' / 'style_guide.md')}\n\n"
            f"APPROVED REFERENCES\n{cli.relevant_reference(chunk['source'])}\n\n"
            f"PREVIOUS FINALIZED TARGET TAIL\n{previous}\n\nSOURCE\n{chunk['source']}\n\n"
            f"BILINGUAL REVIEW\n{json.dumps(review, ensure_ascii=False)}\n\nREVIEWED CANDIDATE\n{candidate}"
        )
    elif stage == "assemble":
        body = "Run `book-translate assemble` and verify that every finalized chunk is included in source order."
    elif stage == "quality":
        body = "Run `book-translate qa`; do not weaken or bypass any formal release-gate error."
    elif stage == "book_audit":
        body = "Audit chapter-transition continuity and previously reviewed material. Return strict JSON using prompts/book_audit.md."
    elif stage == "notion":
        body = "Run the configured Notion sync only after the formal quality task has succeeded."
    return (
        f"# Durable Codex task: {task['task_id']}\n\n"
        f"Write the result to `{output}`. Do not modify unrelated artifacts.\n\n"
        f"## Instructions\n\n{instructions}\n\n## Bounded task input\n\n{body}\n"
    )


def claim_codex_task(root: Path, run: RunStore, worker_id: str) -> Optional[Dict[str, Any]]:
    lease = run.claim(worker_id, lease_seconds=3600)
    if lease is None:
        return None
    task = run.task(lease.task_id)
    output = expected_output(root, task)
    packet_dir = root / "build" / "packets" / run.run_id
    packet_dir.mkdir(parents=True, exist_ok=True)
    packet_path = packet_dir / f"{lease.task_id.replace(':', '--')}.md"
    packet_path.write_text(_codex_packet(root, task, output, run), encoding="utf-8")
    descriptor = {
        "run_id": run.run_id,
        "task_id": lease.task_id,
        "stage": lease.stage,
        "payload": dict(lease.payload),
        "worker_id": worker_id,
        "attempt": lease.attempt,
        "lease_expires_at": lease.lease_expires_at,
        "expected_output": str(output),
        "packet": str(packet_path),
        "instructions": str(root / "prompts" / f"{lease.stage}.md"),
    }
    lease_dir = run.path / "leases"
    lease_dir.mkdir(exist_ok=True)
    (lease_dir / f"{worker_id}.json").write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return descriptor


def complete_codex_task(root: Path, run: RunStore, task_id: str, worker_id: str) -> Path:
    """Validate a Codex-produced artifact, add provenance, and commit its lease."""

    from . import cli

    cli.ROOT = root
    task = run.task(task_id)
    output = expected_output(root, task)
    if not output.exists():
        raise FileNotFoundError(f"Expected task output does not exist: {output}")
    stage = task["stage"]
    chunk_id = str(task["payload"].get("chunk_id", ""))
    if stage in {"metadata", "terminology", "terminology_consolidate", "review", "quality", "book_audit"}:
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object in {output}")
    if stage == "terminology_consolidate":
        _write_approved_references(root, value)
    elif stage == "quality" and (value.get("passed") is not True or value.get("error_count")):
        raise ValueError("Cannot complete a failed formal quality gate")
    elif stage == "book_audit" and (value.get("verdict") != "pass" or value.get("issues")):
        raise ValueError("Cannot complete a book audit that requires revision")
    elif stage == "translate":
        chunk = _chunk(root, chunk_id)
        _validate_codex_target(root, chunk, output)
        cli.write_json(
            root / "output" / "chunks" / f"{chunk_id}.meta.json",
            {
                "chunk_id": chunk_id,
                "source_sha256": chunk["source_sha256"],
                "translation_sha256": sha256_file(output),
                "engine": "codex",
                "input_hash": task["input_hash"],
            },
        )
    elif stage == "review":
        chunk = _chunk(root, chunk_id)
        draft = root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
        if value.get("verdict") not in {"pass", "revise", "escalate"} or not isinstance(value.get("issues"), list):
            raise ValueError("Review must contain a valid verdict and issues list")
        if value.get("verdict") == "revise" and not value.get("corrected_translation"):
            raise ValueError("A revise review must include the complete corrected_translation")
        value.update({"source_sha256": chunk["source_sha256"], "translation_sha256": sha256_file(draft)})
        cli.write_json(output, value)
    elif stage == "finalize":
        chunk = _chunk(root, chunk_id)
        _validate_codex_target(root, chunk, output)
        draft = root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
        review = root / "output" / "reviews" / f"{chunk_id}.review.json"
        cli.write_json(
            root / "output" / "chunks" / f"{chunk_id}.final.meta.json",
            {
                "chunk_id": chunk_id,
                "source_sha256": chunk["source_sha256"],
                "draft_sha256": sha256_file(draft),
                "review_sha256": sha256_file(review),
                "translation_sha256": sha256_file(output),
                "engine": "codex",
                "input_hash": task["input_hash"],
            },
        )
    run.succeed(task_id, worker_id, sha256_file(output), model=task.get("model"))
    return output


def _validate_codex_target(root: Path, chunk: Mapping[str, Any], output: Path) -> None:
    """Reject structurally incomplete or visibly unsafe Codex prose before queue commit."""

    from .quality import HIGH_CONFIDENCE_SIMPLIFIED, PLACEHOLDER_RE

    target = output.read_text(encoding="utf-8")
    if not target.strip():
        raise ValueError("Translation output is empty")
    source_paragraphs = [part for part in re.split(r"\n\s*\n", str(chunk["source"]).strip()) if part.strip()]
    target_paragraphs = [part for part in re.split(r"\n\s*\n", target.strip()) if part.strip()]
    if len(source_paragraphs) != len(target_paragraphs):
        cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
        exception = (cfg.get("quality", {}).get("paragraph_count_exceptions", {}) or {}).get(chunk["chunk_id"])
        allowed = (
            isinstance(exception, Mapping)
            and exception.get("source") == len(source_paragraphs)
            and exception.get("target") == len(target_paragraphs)
            and bool(exception.get("reason"))
        )
        if not allowed:
            raise ValueError(
                f"Paragraph count mismatch for {chunk['chunk_id']}: source={len(source_paragraphs)} target={len(target_paragraphs)}"
            )
    source_first = source_paragraphs[0].strip() if source_paragraphs else ""
    if source_first and re.fullmatch(r"(?i)(?:chapter\s+)?(?:[ivxlcdm]+|[a-z]+(?:-[a-z]+)?)", source_first):
        if not target_paragraphs or not target_paragraphs[0].lstrip().startswith("#"):
            raise ValueError(f"Chapter heading is not Markdown H1 in {chunk['chunk_id']}")
    simplified = sorted({char for char in target if char in HIGH_CONFIDENCE_SIMPLIFIED})
    if simplified:
        raise ValueError(f"High-confidence Simplified Chinese in {chunk['chunk_id']}: {''.join(simplified)}")
    placeholder = PLACEHOLDER_RE.search(target)
    if placeholder:
        raise ValueError(f"Unresolved placeholder in {chunk['chunk_id']}: {placeholder.group(0)}")

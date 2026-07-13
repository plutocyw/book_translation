"""Durable run manifests and a resumable SQLite task queue.

This module deliberately contains no model or CLI code.  It is the persistence
layer used by either a Codex-driven coordinator or an unattended API worker
pool.  A run manifest captures the immutable inputs used to create a run, while
``RunStore`` owns mutable task state in SQLite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "state.sqlite3"

TASK_STATES = (
    "pending",
    "ready",
    "leased",
    "succeeded",
    "retryable_failed",
    "blocked",
    "stale",
)

LEGAL_TRANSITIONS = {
    "pending": {"ready", "blocked", "stale"},
    "ready": {"leased", "blocked", "stale"},
    "leased": {"ready", "succeeded", "retryable_failed", "blocked", "stale"},
    "succeeded": {"stale"},
    "retryable_failed": {"ready", "blocked", "stale"},
    "blocked": {"pending", "ready", "stale"},
    "stale": {"pending", "ready", "blocked"},
}


class OrchestratorError(RuntimeError):
    """Base class for persistent orchestration failures."""


class InvalidTransition(OrchestratorError):
    """Raised when a task state transition is not legal."""


class LeaseError(OrchestratorError):
    """Raised when a worker does not own the active task lease."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    """Return a stable SHA-256 for a JSON-compatible value."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def sha256_file(path: Union[str, Path]) -> str:
    """Hash a file without loading it fully into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_fingerprint(path: Union[str, Path]) -> Dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    stat = resolved.stat()
    if not resolved.is_file():
        raise ValueError("Expected a file: {}".format(resolved))
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size": stat.st_size,
    }


def _config_fingerprint(
    config: Optional[Union[str, Path, Mapping[str, Any]]]
) -> Dict[str, Any]:
    if config is None:
        return {"kind": "inline", "value": {}, "sha256": sha256_json({})}
    if isinstance(config, Mapping):
        value = dict(config)
        return {"kind": "inline", "value": value, "sha256": sha256_json(value)}
    result = _file_fingerprint(config)
    result["kind"] = "file"
    return result


def _fingerprint_group(
    values: Optional[Mapping[str, Union[str, Path]]]
) -> Dict[str, Dict[str, Any]]:
    return {
        name: _file_fingerprint(path)
        for name, path in sorted((values or {}).items())
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(str(temporary), str(path))


@dataclass(frozen=True)
class TaskSpec:
    """Specification for a task inserted into a run queue."""

    task_id: str
    stage: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    dependencies: Sequence[str] = field(default_factory=tuple)
    input_hash: Optional[str] = None
    sequence: int = 0
    priority: int = 0
    max_attempts: int = 3
    model: Optional[str] = None


@dataclass(frozen=True)
class TaskLease:
    """A task claimed by one worker until ``lease_expires_at``."""

    task_id: str
    stage: str
    payload: Mapping[str, Any]
    input_hash: str
    sequence: int
    priority: int
    attempt: int
    max_attempts: int
    model: Optional[str]
    worker_id: str
    lease_expires_at: float


class RunStore:
    """A run's immutable manifest and mutable, concurrency-safe task queue."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path).expanduser().resolve()
        self.manifest_path = self.path / MANIFEST_NAME
        self.database_path = self.path / DATABASE_NAME
        if not self.manifest_path.is_file() or not self.database_path.is_file():
            raise FileNotFoundError("Not an orchestrator run: {}".format(self.path))
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            raise OrchestratorError(
                "Unsupported manifest schema version: {}".format(
                    self.manifest.get("schema_version")
                )
            )
        recorded_hash = self.manifest.get("manifest_sha256")
        hashable_manifest = dict(self.manifest)
        hashable_manifest.pop("manifest_sha256", None)
        if not recorded_hash or recorded_hash != sha256_json(hashable_manifest):
            raise OrchestratorError("Manifest integrity check failed: {}".format(self.manifest_path))

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def verify_manifest_inputs(self) -> Dict[str, Any]:
        """Report files that changed or disappeared since run creation.

        An empty result means every file-backed source, config, prompt, and
        reference still matches the immutable manifest.  Inline config is
        already represented by its canonical hash and needs no filesystem check.
        """

        drift: Dict[str, Any] = {}
        groups = [("source", {"source": self.manifest["source"]})]
        if self.manifest["config"].get("kind") == "file":
            groups.append(("config", {"config": self.manifest["config"]}))
        groups.extend(
            (name, self.manifest[name]) for name in ("prompts", "references")
        )
        for group_name, entries in groups:
            for name, recorded in entries.items():
                path = Path(recorded["path"])
                key = group_name if name == group_name else "{}.{}".format(group_name, name)
                if not path.is_file():
                    drift[key] = {"expected": recorded["sha256"], "actual": None}
                    continue
                actual = sha256_file(path)
                if actual != recorded["sha256"]:
                    drift[key] = {"expected": recorded["sha256"], "actual": actual}
        return drift

    def add_task(self, spec: TaskSpec) -> None:
        """Add one task.  Its dependencies must already exist."""

        self.add_tasks([spec])

    def add_tasks(self, specs: Iterable[TaskSpec]) -> None:
        """Atomically add tasks and dependency edges.

        Batch insertion allows tasks in the same batch to depend on each other.
        Tasks with no dependencies become ``ready`` immediately; all others are
        initially ``pending`` and are promoted after their prerequisites pass.
        """

        items = list(specs)
        if not items:
            return
        seen = set()
        for spec in items:
            if not spec.task_id or spec.task_id in seen:
                raise ValueError("Task IDs must be non-empty and unique within a batch")
            seen.add(spec.task_id)
            if not spec.stage:
                raise ValueError("Task stage must be non-empty")
            if spec.max_attempts < 1:
                raise ValueError("max_attempts must be at least one")

        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for spec in items:
                payload_json = _canonical_json(dict(spec.payload)).decode("utf-8")
                input_hash = spec.input_hash or sha256_json(dict(spec.payload))
                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, stage, sequence_no, priority, state, payload_json,
                        input_hash, max_attempts, model, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.task_id,
                        spec.stage,
                        spec.sequence,
                        spec.priority,
                        payload_json,
                        input_hash,
                        spec.max_attempts,
                        spec.model,
                        now,
                        now,
                    ),
                )
            for spec in items:
                for dependency in spec.dependencies:
                    if dependency == spec.task_id:
                        raise ValueError("A task cannot depend on itself")
                    connection.execute(
                        "INSERT INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                        (spec.task_id, dependency),
                    )
            self._promote_ready(connection, now)

    @staticmethod
    def _promote_ready(connection: sqlite3.Connection, now: float) -> int:
        cursor = connection.execute(
            """
            UPDATE tasks
               SET state = 'ready', updated_at = ?
             WHERE state = 'pending'
               AND NOT EXISTS (
                    SELECT 1
                      FROM task_dependencies d
                      JOIN tasks prerequisite ON prerequisite.task_id = d.depends_on
                     WHERE d.task_id = tasks.task_id
                       AND prerequisite.state != 'succeeded'
               )
            """,
            (now,),
        )
        return cursor.rowcount

    @staticmethod
    def _reclaim_expired(connection: sqlite3.Connection, now: float) -> Dict[str, int]:
        blocked = connection.execute(
            """
            UPDATE tasks
               SET state = 'blocked',
                   error = 'lease expired after final attempt',
                   lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                   updated_at = ?
             WHERE state = 'leased' AND lease_expires_at <= ?
               AND attempt >= max_attempts
            """,
            (now, now),
        ).rowcount
        ready = connection.execute(
            """
            UPDATE tasks
               SET state = 'ready', error = 'lease expired; reclaimed',
                   lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                   updated_at = ?
             WHERE state = 'leased' AND lease_expires_at <= ?
               AND attempt < max_attempts
            """,
            (now, now),
        ).rowcount
        return {"ready": ready, "blocked": blocked}

    def reclaim_expired(self, now: Optional[float] = None) -> Dict[str, int]:
        """Return expired leases to ``ready`` or block exhausted tasks."""

        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reclaim_expired(connection, timestamp)

    def claim(
        self,
        worker_id: str,
        stages: Optional[Sequence[str]] = None,
        lease_seconds: float = 300.0,
        now: Optional[float] = None,
    ) -> Optional[TaskLease]:
        """Atomically claim the highest-priority ready task.

        ``BEGIN IMMEDIATE`` serializes selection and update, so concurrent worker
        processes cannot receive the same task.  Expired leases are reclaimed as
        part of the same transaction.
        """

        if not worker_id:
            raise ValueError("worker_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = time.time() if now is None else now
        expires_at = timestamp + lease_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reclaim_expired(connection, timestamp)
            self._promote_ready(connection, timestamp)
            active_leases = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE state = 'leased'"
            ).fetchone()["count"]
            if active_leases >= self.manifest["concurrency"]:
                return None
            query = "SELECT * FROM tasks WHERE state = 'ready'"
            parameters: List[Any] = []
            if stages is not None:
                if not stages:
                    return None
                placeholders = ",".join("?" for _ in stages)
                query += " AND stage IN ({})".format(placeholders)
                parameters.extend(stages)
            query += " ORDER BY priority DESC, sequence_no ASC, created_at ASC, task_id ASC LIMIT 1"
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                """
                UPDATE tasks
                   SET state = 'leased', attempt = attempt + 1,
                       lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?,
                       error = NULL, updated_at = ?
                 WHERE task_id = ? AND state = 'ready'
                """,
                (worker_id, expires_at, timestamp, timestamp, row["task_id"]),
            ).rowcount
            if changed != 1:  # Defensive; BEGIN IMMEDIATE should make this impossible.
                raise OrchestratorError("Atomic task claim lost its selected row")
            return TaskLease(
                task_id=row["task_id"],
                stage=row["stage"],
                payload=json.loads(row["payload_json"]),
                input_hash=row["input_hash"],
                sequence=row["sequence_no"],
                priority=row["priority"],
                attempt=row["attempt"] + 1,
                max_attempts=row["max_attempts"],
                model=row["model"],
                worker_id=worker_id,
                lease_expires_at=expires_at,
            )

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection, task_id: str, worker_id: str, now: float
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["state"] != "leased" or row["lease_owner"] != worker_id:
            raise LeaseError("Worker {!r} does not own task {!r}".format(worker_id, task_id))
        if row["lease_expires_at"] <= now:
            raise LeaseError("Lease for task {!r} has expired".format(task_id))
        return row

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        lease_seconds: float = 300.0,
        now: Optional[float] = None,
    ) -> float:
        """Extend an owned, unexpired lease and return its new expiry."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = time.time() if now is None else now
        expires_at = timestamp + lease_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, worker_id, timestamp)
            connection.execute(
                """
                UPDATE tasks SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                 WHERE task_id = ?
                """,
                (expires_at, timestamp, timestamp, task_id),
            )
        return expires_at

    def succeed(
        self,
        task_id: str,
        worker_id: str,
        output_hash: str,
        usage: Optional[Mapping[str, Union[int, float]]] = None,
        model: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """Commit successful output metadata and release the lease."""

        if not output_hash:
            raise ValueError("output_hash must be non-empty")
        timestamp = time.time() if now is None else now
        usage_json = _canonical_json(dict(usage or {})).decode("utf-8")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_lease(connection, task_id, worker_id, timestamp)
            self._assert_transition(row["state"], "succeeded")
            connection.execute(
                """
                UPDATE tasks
                   SET state = 'succeeded', output_hash = ?, usage_json = ?,
                       model = COALESCE(?, model), error = NULL,
                       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                       updated_at = ?
                 WHERE task_id = ?
                """,
                (output_hash, usage_json, model, timestamp, task_id),
            )
            self._promote_ready(connection, timestamp)

    def fail(
        self,
        task_id: str,
        worker_id: str,
        error: str,
        retryable: bool = True,
        usage: Optional[Mapping[str, Union[int, float]]] = None,
        model: Optional[str] = None,
        now: Optional[float] = None,
    ) -> str:
        """Record an owned attempt failure and return the resulting state."""

        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_lease(connection, task_id, worker_id, timestamp)
            target = (
                "retryable_failed"
                if retryable and row["attempt"] < row["max_attempts"]
                else "blocked"
            )
            self._assert_transition(row["state"], target)
            connection.execute(
                """
                UPDATE tasks
                   SET state = ?, error = ?, usage_json = ?, model = COALESCE(?, model),
                       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                       updated_at = ?
                 WHERE task_id = ?
                """,
                (
                    target,
                    error,
                    _canonical_json(dict(usage or {})).decode("utf-8"),
                    model,
                    timestamp,
                    task_id,
                ),
            )
        return target

    def retry(self, task_id: str, now: Optional[float] = None) -> None:
        """Make a retryable failed task available for another claim."""

        self.transition(task_id, "ready", now=now)

    @staticmethod
    def _assert_transition(old_state: str, new_state: str) -> None:
        if new_state not in LEGAL_TRANSITIONS.get(old_state, set()):
            raise InvalidTransition("Illegal task transition: {} -> {}".format(old_state, new_state))

    def transition(
        self, task_id: str, new_state: str, now: Optional[float] = None
    ) -> None:
        """Perform a legal non-lease administrative state transition."""

        if new_state not in TASK_STATES:
            raise ValueError("Unknown task state: {}".format(new_state))
        if new_state in ("leased", "succeeded", "retryable_failed"):
            raise InvalidTransition("Use claim/succeed/fail for state {}".format(new_state))
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            self._assert_transition(row["state"], new_state)
            connection.execute(
                """
                UPDATE tasks
                   SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                       heartbeat_at = NULL, updated_at = ?
                 WHERE task_id = ?
                """,
                (new_state, timestamp, task_id),
            )

    def mark_stale(
        self, task_id: str, cascade: bool = True, now: Optional[float] = None
    ) -> List[str]:
        """Invalidate a task and, by default, every transitive dependent.

        Active leases are intentionally rejected.  The coordinator should first
        let the worker finish or administratively block it, preventing an old
        worker from publishing output after invalidation.
        """

        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if cascade:
                rows = connection.execute(
                    """
                    WITH RECURSIVE downstream(task_id) AS (
                        SELECT ?
                        UNION
                        SELECT d.task_id
                          FROM task_dependencies d
                          JOIN downstream ON d.depends_on = downstream.task_id
                    )
                    SELECT t.task_id, t.state
                      FROM tasks t JOIN downstream USING (task_id)
                    """,
                    (task_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT task_id, state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchall()
            if not rows:
                raise KeyError(task_id)
            leased = [row["task_id"] for row in rows if row["state"] == "leased"]
            if leased:
                raise InvalidTransition(
                    "Cannot stale actively leased tasks: {}".format(", ".join(leased))
                )
            changed = []
            for row in rows:
                if row["state"] == "stale":
                    continue
                self._assert_transition(row["state"], "stale")
                connection.execute(
                    """
                    UPDATE tasks
                       SET state = 'stale', output_hash = NULL, error = NULL,
                           lease_owner = NULL, lease_expires_at = NULL,
                           heartbeat_at = NULL, updated_at = ?
                     WHERE task_id = ?
                    """,
                    (timestamp, row["task_id"]),
                )
                changed.append(row["task_id"])
            return changed

    def reset_stale(self, task_id: str, now: Optional[float] = None) -> str:
        """Reset one stale task to ``ready`` or ``pending`` based on dependencies."""

        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            prerequisites_ready = connection.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1 FROM task_dependencies d
                    JOIN tasks prerequisite ON prerequisite.task_id = d.depends_on
                    WHERE d.task_id = ? AND prerequisite.state != 'succeeded'
                ) AS ready
                """,
                (task_id,),
            ).fetchone()["ready"]
            target = "ready" if prerequisites_ready else "pending"
            self._assert_transition(row["state"], target)
            connection.execute(
                """
                UPDATE tasks SET state = ?, attempt = 0, usage_json = '{}',
                    error = NULL, updated_at = ? WHERE task_id = ?
                """,
                (target, timestamp, task_id),
            )
            return target

    def update_input(
        self,
        task_id: str,
        input_hash: str,
        payload: Optional[Mapping[str, Any]] = None,
        now: Optional[float] = None,
    ) -> List[str]:
        """Record changed task input and invalidate all affected outputs."""

        if not input_hash:
            raise ValueError("input_hash must be non-empty")
        current = self.task(task_id)
        if current["input_hash"] == input_hash and payload is None:
            return []
        changed = self.mark_stale(task_id, cascade=True, now=now)
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if payload is None:
                connection.execute(
                    "UPDATE tasks SET input_hash = ?, updated_at = ? WHERE task_id = ?",
                    (input_hash, timestamp, task_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE tasks SET input_hash = ?, payload_json = ?, updated_at = ?
                     WHERE task_id = ?
                    """,
                    (
                        input_hash,
                        _canonical_json(dict(payload)).decode("utf-8"),
                        timestamp,
                        task_id,
                    ),
                )
        return changed

    def task(self, task_id: str) -> Dict[str, Any]:
        """Return one task, decoding JSON metadata."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._decode_task(row)

    def tasks(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return tasks in deterministic execution order, optionally filtered by state."""

        if state is not None and state not in TASK_STATES:
            raise ValueError("Unknown task state: {}".format(state))
        query = "SELECT * FROM tasks"
        parameters: Tuple[Any, ...] = ()
        if state is not None:
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY priority DESC, sequence_no, created_at, task_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode_task(row) for row in rows]

    @staticmethod
    def _decode_task(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["usage"] = json.loads(result.pop("usage_json"))
        return result

    def status(self) -> Dict[str, Any]:
        """Return queue counts, aggregate recorded usage, and manifest drift."""

        with self._connect() as connection:
            counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state"
                )
            }
            rows = connection.execute("SELECT usage_json FROM tasks").fetchall()
        usage: Dict[str, Union[int, float]] = {}
        for row in rows:
            for name, value in json.loads(row["usage_json"]).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[name] = usage.get(name, 0) + value
        return {
            "run_id": self.run_id,
            "engine": self.manifest["engine"],
            "concurrency": self.manifest["concurrency"],
            "total_tasks": sum(counts.values()),
            "by_state": {state: counts.get(state, 0) for state in TASK_STATES},
            "usage": usage,
            "input_drift": self.verify_manifest_inputs(),
        }


def _initialize_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = FULL;

            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                sequence_no INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK (state IN (
                    'pending', 'ready', 'leased', 'succeeded',
                    'retryable_failed', 'blocked', 'stale'
                )),
                payload_json TEXT NOT NULL DEFAULT '{}',
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
                model TEXT,
                usage_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                lease_owner TEXT,
                lease_expires_at REAL,
                heartbeat_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE task_dependencies (
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                depends_on TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
                PRIMARY KEY (task_id, depends_on)
            );

            CREATE INDEX tasks_claim_order
                ON tasks(state, priority DESC, sequence_no, created_at);
            CREATE INDEX tasks_lease_expiry ON tasks(state, lease_expires_at);
            CREATE INDEX dependencies_prerequisite ON task_dependencies(depends_on);
            """
        )
        connection.commit()
    finally:
        connection.close()


def create_run(
    runs_root: Union[str, Path],
    *,
    source: Union[str, Path],
    config: Optional[Union[str, Path, Mapping[str, Any]]] = None,
    prompts: Optional[Mapping[str, Union[str, Path]]] = None,
    references: Optional[Mapping[str, Union[str, Path]]] = None,
    engine: str = "codex",
    concurrency: int = 1,
    run_id: Optional[str] = None,
) -> RunStore:
    """Create and open a durable run directory.

    ``prompts`` and ``references`` map stable logical names to files.  ``config``
    may be either a file or an inline JSON-compatible mapping.
    """

    if not engine:
        raise ValueError("engine must be non-empty")
    if concurrency < 1:
        raise ValueError("concurrency must be at least one")
    identifier = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
        raise ValueError("run_id may contain only letters, numbers, '.', '_' and '-'")
    root = Path(runs_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_path = root / identifier
    run_path.mkdir()
    try:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": identifier,
            "created_at": _utc_now(),
            "engine": engine,
            "concurrency": concurrency,
            "source": _file_fingerprint(source),
            "config": _config_fingerprint(config),
            "prompts": _fingerprint_group(prompts),
            "references": _fingerprint_group(references),
        }
        manifest["manifest_sha256"] = sha256_json(manifest)
        _write_json_atomic(run_path / MANIFEST_NAME, manifest)
        _initialize_database(run_path / DATABASE_NAME)
    except Exception:
        for child in run_path.iterdir():
            child.unlink()
        run_path.rmdir()
        raise
    return RunStore(run_path)


def open_run(path: Union[str, Path], run_id: Optional[str] = None) -> RunStore:
    """Open either a run directory or ``runs_root/run_id``."""

    resolved = Path(path).expanduser()
    if run_id is not None:
        resolved = resolved / run_id
    return RunStore(resolved)

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from translation_pipeline.orchestrator import (
    InvalidTransition,
    LeaseError,
    OrchestratorError,
    TaskSpec,
    create_run,
    open_run,
)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "book.pdf"
        self.config = self.root / "project.json"
        self.prompt = self.root / "translate.md"
        self.reference = self.root / "style.md"
        self.source.write_bytes(b"pdf source")
        self.config.write_text('{"locale":"zh-Hant-TW"}', encoding="utf-8")
        self.prompt.write_text("Translate faithfully.", encoding="utf-8")
        self.reference.write_text("Use Traditional Chinese.", encoding="utf-8")
        self.runs = self.root / "runs"

    def tearDown(self):
        self.temporary.cleanup()

    def make_run(self, run_id="test-run", concurrency=3):
        return create_run(
            self.runs,
            run_id=run_id,
            source=self.source,
            config=self.config,
            prompts={"translate": self.prompt},
            references={"style": self.reference},
            engine="codex",
            concurrency=concurrency,
        )

    def test_manifest_records_all_immutable_input_hashes(self):
        run = self.make_run()
        manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["engine"], "codex")
        self.assertEqual(manifest["concurrency"], 3)
        self.assertEqual(
            manifest["source"]["sha256"], hashlib.sha256(b"pdf source").hexdigest()
        )
        self.assertIn("sha256", manifest["config"])
        self.assertIn("sha256", manifest["prompts"]["translate"])
        self.assertIn("sha256", manifest["references"]["style"])
        self.assertEqual(open_run(self.runs, "test-run").run_id, "test-run")

    def test_open_rejects_a_modified_manifest(self):
        run = self.make_run()
        manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
        manifest["concurrency"] = 99
        run.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(OrchestratorError):
            open_run(run.path)

    def test_manifest_drift_is_reported(self):
        run = self.make_run()
        self.assertEqual(run.verify_manifest_inputs(), {})
        self.prompt.write_text("Changed prompt", encoding="utf-8")
        drift = run.status()["input_drift"]
        self.assertIn("prompts.translate", drift)
        self.assertNotEqual(
            drift["prompts.translate"]["expected"],
            drift["prompts.translate"]["actual"],
        )

    def test_dependencies_promote_tasks_in_order(self):
        run = self.make_run()
        run.add_tasks(
            [
                TaskSpec("extract", "extract", {"page": 1}),
                TaskSpec("draft", "translate", dependencies=("extract",)),
                TaskSpec("review", "review", dependencies=("draft",)),
            ]
        )
        self.assertEqual(run.status()["by_state"]["ready"], 1)
        self.assertEqual(run.status()["by_state"]["pending"], 2)
        first = run.claim("worker", now=10, lease_seconds=10)
        self.assertEqual(first.task_id, "extract")
        run.succeed("extract", "worker", "source-hash", now=11)
        self.assertEqual(run.task("draft")["state"], "ready")
        self.assertEqual(run.task("review")["state"], "pending")

    def test_claim_is_atomic_across_concurrent_workers(self):
        run = self.make_run(concurrency=8)
        run.add_tasks(TaskSpec("task-{}".format(i), "draft") for i in range(8))
        claimed = []
        lock = threading.Lock()

        def worker(index):
            lease = run.claim("worker-{}".format(index), lease_seconds=30)
            with lock:
                claimed.append(lease.task_id if lease else None)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(claimed)), 8)
        self.assertNotIn(None, claimed)

    def test_claim_enforces_run_concurrency(self):
        run = self.make_run(concurrency=2)
        run.add_tasks(TaskSpec("task-{}".format(i), "draft") for i in range(3))
        self.assertIsNotNone(run.claim("one", now=0, lease_seconds=10))
        self.assertIsNotNone(run.claim("two", now=0, lease_seconds=10))
        self.assertIsNone(run.claim("three", now=0, lease_seconds=10))
        self.assertEqual(run.status()["by_state"]["ready"], 1)

    def test_heartbeat_and_lease_owner_are_enforced(self):
        run = self.make_run()
        run.add_task(TaskSpec("draft", "translate"))
        lease = run.claim("one", now=100, lease_seconds=10)
        self.assertEqual(lease.lease_expires_at, 110)
        self.assertEqual(run.heartbeat("draft", "one", now=105, lease_seconds=20), 125)
        with self.assertRaises(LeaseError):
            run.succeed("draft", "two", "hash", now=106)
        with self.assertRaises(LeaseError):
            run.heartbeat("draft", "one", now=126)

    def test_expired_lease_is_reclaimed_and_attempt_is_incremented(self):
        run = self.make_run()
        run.add_task(TaskSpec("draft", "translate", max_attempts=2))
        self.assertEqual(run.claim("one", now=0, lease_seconds=5).attempt, 1)
        replacement = run.claim("two", now=6, lease_seconds=5)
        self.assertEqual(replacement.task_id, "draft")
        self.assertEqual(replacement.attempt, 2)
        self.assertEqual(run.reclaim_expired(now=12), {"ready": 0, "blocked": 1})
        self.assertEqual(run.task("draft")["state"], "blocked")

    def test_failure_retry_usage_and_output_metadata(self):
        run = self.make_run()
        run.add_task(TaskSpec("draft", "translate", model="terra"))
        run.claim("one", now=0, lease_seconds=10)
        state = run.fail(
            "draft",
            "one",
            "temporary",
            usage={"input_tokens": 100},
            now=1,
        )
        self.assertEqual(state, "retryable_failed")
        run.retry("draft", now=2)
        run.claim("two", now=3, lease_seconds=10)
        run.succeed(
            "draft",
            "two",
            "translation-hash",
            usage={"input_tokens": 40, "output_tokens": 20},
            now=4,
        )
        task = run.task("draft")
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(task["output_hash"], "translation-hash")
        self.assertEqual(task["attempt"], 2)
        self.assertEqual(run.status()["usage"]["output_tokens"], 20)

    def test_stale_cascades_and_resets_using_dependencies(self):
        run = self.make_run()
        run.add_tasks(
            [
                TaskSpec("draft", "translate"),
                TaskSpec("review", "review", dependencies=("draft",)),
                TaskSpec("publish", "publish", dependencies=("review",)),
            ]
        )
        for task_id in ("draft", "review", "publish"):
            lease = run.claim("worker", now=10, lease_seconds=10)
            self.assertEqual(lease.task_id, task_id)
            run.succeed(task_id, "worker", task_id + "-hash", now=11)
        changed = run.update_input("draft", "new-input-hash", {"version": 2}, now=20)
        self.assertEqual(set(changed), {"draft", "review", "publish"})
        self.assertEqual(run.reset_stale("review", now=21), "pending")
        self.assertEqual(run.reset_stale("draft", now=21), "ready")
        self.assertIsNone(run.task("draft")["output_hash"])

    def test_illegal_transition_is_rejected(self):
        run = self.make_run()
        run.add_task(TaskSpec("draft", "translate"))
        with self.assertRaises(InvalidTransition):
            run.transition("draft", "succeeded")
        with self.assertRaises(InvalidTransition):
            run.transition("draft", "pending")


if __name__ == "__main__":
    unittest.main()

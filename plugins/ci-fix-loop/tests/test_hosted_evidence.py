import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
SPEC = importlib.util.spec_from_file_location("ci_hosted_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HostedEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "job.log"

    def preflight(self, text):
        self.path.write_text(text, encoding="utf-8")
        return {"check_snapshot": {
            "failures": [{
                "key": "check:Build/tests",
                "url": "https://github.com/owner/repo/actions/runs/11/job/22",
                "log_path": str(self.path), "log_sha256": MODULE.sha256_text(text),
            }],
            "workflow_runs": {"11": {
                "id": 11, "workflow_id": 33, "head_sha": "a" * 40, "run_attempt": 3,
                "status": "completed", "conclusion": "failure", "name": "Build",
            }},
        }}

    def test_complete_small_log_preserves_class_loading_error(self):
        text = (
            "legacyProtostellarUnitTests\n"
            "java.lang.NoClassDefFoundError: com/couchbase/client/core/util/CouchbaseConnectionStrings\n"
            "Caused by: java.lang.ClassNotFoundException: CouchbaseConnectionStrings\n"
        )
        evidence = json.loads(MODULE.controller_ci_evidence(self.preflight(text)))
        record = evidence["logs"][0]
        self.assertEqual(text, record["text"])
        self.assertEqual(3, record["run"]["run_attempt"])
        self.assertEqual(MODULE.sha256_text(text), record["log_sha256"])
        self.assertNotIn(str(self.path), json.dumps(evidence))

    def test_large_logs_use_exact_retrieval_instead_of_losing_error_positions(self):
        for position in ("first", "middle", "last"):
            with self.subTest(position=position):
                padding = "routine build output\n" * 4000
                error = "NoClassDefFoundError: CouchbaseConnectionStrings\n"
                text = error + padding if position == "first" else (
                    padding + error if position == "last" else padding + error + padding
                )
                rendered = MODULE.controller_ci_evidence(self.preflight(text))
                record = json.loads(rendered)["logs"][0]
                self.assertLessEqual(len(rendered.encode("utf-8")), MODULE.MAX_TRIAGE_SUMMARY_BYTES)
                self.assertTrue(record["retrieve_full_log"])
                self.assertNotIn("text", record)
                self.assertEqual(len(text.encode("utf-8")), record["omitted_utf8_bytes"])
                self.assertEqual(3, record["run"]["run_attempt"])
                self.assertEqual(22, record["job"]["job_id"])
                self.assertEqual(MODULE.sha256_text(text), record["log_sha256"])

    def test_missing_attempt_or_job_never_silently_truncates(self):
        source = self.preflight("x" * (MODULE.MAX_TRIAGE_SUMMARY_BYTES + 1))
        for field in ("job", "attempt"):
            with self.subTest(field=field):
                preflight = copy.deepcopy(source)
                if field == "job":
                    preflight["check_snapshot"]["failures"][0]["url"] = "https://example.test/check"
                else:
                    preflight["check_snapshot"]["workflow_runs"]["11"].pop("run_attempt")
                with self.assertRaisesRegex(MODULE.WorkflowError, "exact job/attempt"):
                    MODULE.controller_ci_evidence(preflight)

    def test_log_drift_and_unavailable_log_stop_before_dispatch(self):
        preflight = self.preflight("original failure\n")
        self.path.write_text("different log", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "identity changed"):
            MODULE.controller_ci_evidence(preflight)
        self.path.unlink()
        with self.assertRaisesRegex(MODULE.WorkflowError, "unavailable"):
            MODULE.controller_ci_evidence(preflight)

    def test_redaction_keeps_error_without_credentials(self):
        token = "ghp_" + "x" * 40
        evidence = MODULE.controller_ci_evidence(
            self.preflight(f"Authorization: Bearer {token}\nCouchbaseConnectionStrings failed\n")
        )
        self.assertNotIn(token, evidence)
        self.assertIn("CouchbaseConnectionStrings failed", evidence)

    def test_oversized_identity_set_fails_explicitly(self):
        preflight = self.preflight("error")
        entry = preflight["check_snapshot"]["failures"][0]
        preflight["check_snapshot"]["failures"] = [
            {**entry, "key": f"check:{index}:" + "x" * 512} for index in range(200)
        ]
        with self.assertRaisesRegex(MODULE.WorkflowError, "identities exceed"):
            MODULE.controller_ci_evidence(preflight)

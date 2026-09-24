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
        self.path.write_text(text, encoding="utf-8", newline="\n")
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
            "head_sha": "a" * 40,
        }}

    def test_small_log_sends_failure_context_not_complete_log(self):
        text = (
            "routine line\n" * 20
            + "CouchbaseProtostellarTargetsTest.preservesConfiguredTargetForLegacyCore FAILED\n"
            "java.lang.NoClassDefFoundError: com/couchbase/client/core/util/CouchbaseConnectionStrings\n"
            "Caused by: java.lang.ClassNotFoundException: CouchbaseConnectionStrings\n"
            + "routine line\n" * 20
        )
        evidence = json.loads(MODULE.controller_ci_evidence(self.preflight(text)))
        record = evidence["logs"][0]
        excerpt = evidence["excerpts"][0]
        self.assertIn("CouchbaseProtostellarTargetsTest", excerpt["text"])
        self.assertIn("NoClassDefFoundError", excerpt["text"])
        self.assertNotIn("routine line\n" * 20, excerpt["text"])
        self.assertEqual("a" * 40, record["head_sha"])
        self.assertEqual(3, record["run"]["run_attempt"])
        self.assertEqual(MODULE.sha256_text(text), record["log_sha256"])
        self.assertNotIn(str(self.path), json.dumps(evidence))

    def test_large_logs_include_errors_at_any_position(self):
        for position in ("first", "middle", "last"):
            with self.subTest(position=position):
                padding = "routine build output\n" * 4000
                error = "NoClassDefFoundError: CouchbaseConnectionStrings\n"
                text = error + padding if position == "first" else (
                    padding + error if position == "last" else padding + error + padding
                )
                evidence = json.loads(MODULE.controller_ci_evidence(self.preflight(text)))
                record = evidence["logs"][0]
                self.assertLessEqual(
                    len(json.dumps(evidence).encode("utf-8")),
                    MODULE.MAX_INLINE_CI_EVIDENCE_BYTES,
                )
                self.assertNotIn("text", record)
                self.assertNotIn("retrieve_full_log", record)
                self.assertIn("NoClassDefFoundError", evidence["excerpts"][0]["text"])
                self.assertEqual(len(text.encode("utf-8")), record["utf8_bytes"])
                self.assertEqual(3, record["run"]["run_attempt"])
                self.assertEqual(22, record["job"]["job_id"])
                self.assertEqual(MODULE.sha256_text(text), record["log_sha256"])

    def test_debug_noise_does_not_displace_deep_test_failure(self):
        noise = (
            "2026-09-24T03:18:40.6294963Z DEBUG Failed to propagate context "
            "because previous context is set\n"
        ) * 4000
        failure = (
            "2026-09-24T03:38:46.8853740Z CouchbaseProtostellarTargetsTest > "
            "preservesConfiguredTargetForLegacyCore() FAILED\n"
            "2026-09-24T03:38:46.8854726Z     java.lang.NoClassDefFoundError: "
            "io/opentelemetry/javaagent/instrumentation/couchbase/common/v3_1/"
            "CouchbaseConnectionStrings\n"
        )
        evidence = json.loads(MODULE.controller_ci_evidence(
            self.preflight(noise + failure + noise)
        ))
        self.assertEqual(8002, evidence["logs"][0]["line_count"])
        self.assertTrue(any(
            "NoClassDefFoundError" in excerpt["text"]
            and "CouchbaseProtostellarTargetsTest" in excerpt["text"]
            for excerpt in evidence["excerpts"]
        ))

    def test_missing_attempt_or_job_remains_visible_without_retrieval(self):
        source = self.preflight("routine output\n" * 4000)
        for field in ("job", "attempt"):
            with self.subTest(field=field):
                preflight = copy.deepcopy(source)
                if field == "job":
                    preflight["check_snapshot"]["failures"][0]["url"] = "https://example.test/check"
                else:
                    preflight["check_snapshot"]["workflow_runs"]["11"].pop("run_attempt")
                record = json.loads(MODULE.controller_ci_evidence(preflight))["logs"][0]
                if field == "job":
                    self.assertIsNone(record["job"])
                else:
                    self.assertNotIn("run_attempt", record["run"])

    def test_log_drift_and_unavailable_log_stop_before_dispatch(self):
        preflight = self.preflight("original failure\n")
        self.path.write_text("different log", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "identity changed"):
            MODULE.controller_ci_evidence(preflight)
        self.path.unlink()
        with self.assertRaisesRegex(MODULE.WorkflowError, "unavailable"):
            MODULE.controller_ci_evidence(preflight)

    def test_identical_errors_are_shared_across_checks(self):
        preflight = self.preflight(
            "WidgetTest.testMethod FAILED\nNoClassDefFoundError: MissingClass\n"
        )
        other = preflight["check_snapshot"]["failures"][0].copy()
        other["key"] = "check:Build/java17"
        preflight["check_snapshot"]["failures"].append(other)
        evidence = json.loads(MODULE.controller_ci_evidence(preflight))
        self.assertEqual(1, len(evidence["excerpts"]))
        self.assertEqual(2, len(evidence["excerpts"][0]["occurrences"]))
        self.assertEqual([0], evidence["logs"][0]["excerpt_ids"])
        self.assertEqual([0], evidence["logs"][1]["excerpt_ids"])

    def test_distinct_exceptions_are_not_grouped(self):
        preflight = self.preflight("WidgetTest.testMethod FAILED\nNoClassDefFoundError: First\n")
        other_path = self.path.with_name("other.log")
        other_text = "WidgetTest.testMethod FAILED\nNoClassDefFoundError: Second\n"
        other_path.write_text(other_text, encoding="utf-8", newline="\n")
        other = preflight["check_snapshot"]["failures"][0].copy()
        other.update(key="check:Build/java17", log_path=str(other_path),
                     log_sha256=MODULE.sha256_text(other_text))
        preflight["check_snapshot"]["failures"].append(other)
        evidence = json.loads(MODULE.controller_ci_evidence(preflight))
        self.assertEqual(2, len(evidence["excerpts"]))

    def test_long_line_is_bounded_and_log_hash_covers_all_bytes(self):
        text = "FAILED " + "x" * 100000 + "\n"
        evidence = json.loads(MODULE.controller_ci_evidence(self.preflight(text)))
        self.assertLess(len(evidence["excerpts"][0]["text"]), 600)
        self.assertEqual(len(text.encode()), evidence["logs"][0]["utf8_bytes"])
        self.assertEqual(MODULE.sha256_text(text), evidence["logs"][0]["log_sha256"])

    def test_oversized_identity_set_fails_explicitly(self):
        preflight = self.preflight("error: build failed\n")
        entry = preflight["check_snapshot"]["failures"][0]
        preflight["check_snapshot"]["failures"] = [
            {**entry, "key": f"check:{index}:" + "x" * 512} for index in range(200)
        ]
        with self.assertRaisesRegex(MODULE.WorkflowError, "identities exceed"):
            MODULE.controller_ci_evidence(preflight)

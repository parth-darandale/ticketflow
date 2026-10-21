"""Guard the demo against reporting success for incomplete or noncausal evidence."""

import copy
import unittest
from unittest.mock import Mock, patch

from scripts.verify_syllabus import restore_workers, validate_trace, validate_recovery_leader, verify


def complete_trace():
    kinds = ["queued", "claimed", "recovered", "claimed", "confirmed", "observed"]
    processes = ["api:one", "worker:old", "worker:new", "worker:new", "worker:new", "api:one"]
    return {"instrumented": True, "events": [
        {"kind": kind, "logical_time": number + 10, "process_id": processes[number],
         "received_time": number + 9 if kind in {"claimed", "recovered", "observed"} else None,
         "ephemeral": kind == "observed"}
        for number, kind in enumerate(kinds)
    ]}


class SyllabusEvidenceTests(unittest.TestCase):
    def test_accepts_complete_causal_recovery_trace(self):
        trace = complete_trace()
        self.assertEqual(validate_trace(trace), trace["events"])

    def test_recovery_actor_and_attempts_match_the_recorded_successor(self):
        events = complete_trace()["events"]
        for event, attempt in zip(events, [0, 1, 1, 2, 2, 2]):
            event["attempt"] = attempt
        events[2]["worker_id"] = "new-container"
        successor = {"owner_id": "worker:new", "worker_id": "new-container"}
        validate_recovery_leader(events, successor)
        with self.assertRaises(AssertionError):
            validate_recovery_leader(events, {**successor, "owner_id": "different-run"})
        events[3]["attempt"] = 1
        with self.assertRaises(AssertionError):
            validate_recovery_leader(events, successor)

    def test_rejects_counter_regression_and_wrong_received_message(self):
        for field, value in (("logical_time", 10), ("received_time", 999)):
            trace = complete_trace()
            trace["events"][1][field] = value
            with self.subTest(field=field), self.assertRaises(AssertionError):
                validate_trace(trace)

    def test_rejects_incomplete_and_same_process_recovery(self):
        original = complete_trace()
        missing_recovery = copy.deepcopy(original)
        missing_recovery["events"].pop(2)
        same_process = copy.deepcopy(original)
        same_process["events"][3]["process_id"] = same_process["events"][1]["process_id"]
        historical = copy.deepcopy(original)
        historical["instrumented"] = False
        for trace in (missing_recovery, same_process, historical):
            with self.subTest(trace=trace), self.assertRaises(AssertionError):
                validate_trace(trace)

    def test_remote_api_is_rejected_before_requests_or_docker(self):
        client = Mock(base_url="https://example.com")
        with patch("scripts.verify_syllabus.worker_containers") as inspect:
            with self.assertRaises(ValueError):
                verify(client)
        client.request.assert_not_called()
        inspect.assert_not_called()

    def test_cleanup_only_unpauses_scoped_recorded_pauses_then_restores(self):
        containers = [
            {"Id": "ours", "State": {"Running": True, "Paused": True}},
            {"Id": "not-paused-by-demo", "State": {"Running": True, "Paused": True}},
        ]
        client = Mock()
        with patch("scripts.verify_syllabus.worker_containers", return_value=containers), \
                patch("scripts.verify_syllabus.docker") as docker, \
                patch("scripts.verify_syllabus.scale_workers") as scale, \
                patch("scripts.verify_syllabus.healthy_workers", return_value=[1, 2, 3]), \
                patch("scripts.verify_syllabus.wait_for_idle_queue") as idle:
            self.assertEqual(restore_workers(client, {"ours", "removed-container"}, 10), [1, 2, 3])
        docker.assert_called_once_with("unpause", "ours")
        scale.assert_called_once_with(3)
        idle.assert_called_once_with(client, 10)


if __name__ == "__main__":
    unittest.main()

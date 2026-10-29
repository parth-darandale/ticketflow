"""Local shutdown authorization and file-signal protocol, without stopping Docker."""

import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


class LocalShutdownTests(unittest.TestCase):
    def setUp(self):
        from app import control
        self.control = control
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {"TICKETFLOW_CONTROL_DIR": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session = str(uuid.uuid4())
        self.write_controller()

    def write_controller(self, **changes):
        value = {"session_id": self.session, "port": 8088, "state": "ready", "updated_at": time.time()}
        value.update(changes)
        (self.root / "controller.json").write_text(json.dumps(value), encoding="utf-8")

    def request(self, origin="http://127.0.0.1:8088", host="127.0.0.1:8088", header="shutdown", site="same-origin"):
        from starlette.requests import Request
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        if header is not None:
            headers.append((b"x-ticketflow-control", header.encode()))
        if site is not None:
            headers.append((b"sec-fetch-site", site.encode()))
        return Request({"type": "http", "method": "POST", "scheme": "http", "path": "/api/system/shutdown",
                        "query_string": b"", "server": ("127.0.0.1", 8088), "headers": headers})

    def test_ready_controller_enables_button_without_exposing_session(self):
        result = self.control.system_control()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["state"], "ready")
        self.assertNotIn(self.session, str(result))

    def test_missing_or_stale_controller_disables_button(self):
        for updated in (time.time() - 30, time.time() + 60):
            self.write_controller(updated_at=updated)
            self.assertFalse(self.control.system_control()["enabled"])
        (self.root / "controller.json").unlink()
        self.assertEqual(self.control.system_control()["state"], "unavailable")

    def test_malformed_controller_is_unavailable(self):
        for invalid in ("broken json", "[]", '{"session_id":"invalid"}', "x" * 9000):
            (self.root / "controller.json").write_text(invalid)
            self.assertFalse(self.control.system_control()["enabled"])

    def test_invalid_fields_and_unknown_state_do_not_enable_control(self):
        for changes in ({"port": True}, {"port": 99999}, {"updated_at": True},
                        {"updated_at": float("nan")}, {"state": "poweroff"}):
            self.write_controller(**changes)
            self.assertFalse(self.control.system_control()["enabled"])

    def test_valid_shutdown_writes_only_fixed_session_request(self):
        result = self.control.request_shutdown(self.control.ShutdownRequest(confirm=True), self.request())
        self.assertTrue(result["accepted"])
        value = json.loads((self.root / "shutdown-request.json").read_text())
        self.assertEqual(value["session_id"], self.session)
        self.assertLess(abs(value["requested_at"] - time.time()), 2)
        self.assertEqual(set(p.name for p in self.root.iterdir()), {"controller.json", "shutdown-request.json"})

    def test_repeated_clicks_are_safe_for_the_same_session(self):
        for state in ("ready", "stopping"):
            self.write_controller(state=state)
            self.control.request_shutdown(self.control.ShutdownRequest(confirm=True), self.request())
        self.assertEqual(json.loads((self.root / "shutdown-request.json").read_text())["session_id"], self.session)

    def test_rejects_cross_origin_missing_headers_and_dns_rebinding(self):
        from fastapi import HTTPException
        cases = [{"origin": None}, {"header": None}, {"origin": "http://evil.example:8088"},
                 {"origin": "http://evil.example:8088", "host": "evil.example:8088"},
                 {"origin": "http://127.0.0.1:8089"}, {"origin": "null"},
                 {"origin": "http://127.0.0.1:8088", "site": "cross-site"}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(HTTPException) as raised:
                self.control.request_shutdown(self.control.ShutdownRequest(confirm=True), self.request(**case))
            self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse((self.root / "shutdown-request.json").exists())

    def test_localhost_alias_requires_matching_origin_and_host(self):
        self.control.request_shutdown(self.control.ShutdownRequest(confirm=True),
                                      self.request(origin="http://localhost:8088", host="localhost:8088"))
        self.assertTrue((self.root / "shutdown-request.json").exists())

    def test_confirmation_required_and_payload_cannot_add_commands(self):
        from fastapi import HTTPException
        from pydantic import ValidationError
        with self.assertRaises(HTTPException) as raised:
            self.control.request_shutdown(self.control.ShutdownRequest(confirm=False), self.request())
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(ValidationError):
            self.control.ShutdownRequest(confirm=True, command="arbitrary")
        with self.assertRaises(ValidationError):
            self.control.ShutdownRequest(confirm="true")
        self.assertFalse((self.root / "shutdown-request.json").exists())

    def test_stopped_or_failed_controller_cannot_accept_shutdown(self):
        from fastapi import HTTPException
        for state in ("stopped", "error"):
            self.write_controller(state=state)
            with self.assertRaises(HTTPException) as raised:
                self.control.request_shutdown(self.control.ShutdownRequest(confirm=True), self.request())
            self.assertEqual(raised.exception.status_code, 503)

    def test_startup_and_direct_compose_without_helper_are_disabled(self):
        self.write_controller(state="starting")
        self.assertFalse(self.control.system_control()["enabled"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.control.system_control()["enabled"])

    def test_new_session_replaces_old_request_without_carrying_commands(self):
        self.control.request_shutdown(self.control.ShutdownRequest(confirm=True), self.request())
        self.session = str(uuid.uuid4())
        self.write_controller()
        self.control.request_shutdown(self.control.ShutdownRequest(confirm=True), self.request())
        value = json.loads((self.root / "shutdown-request.json").read_text())
        self.assertEqual(value["session_id"], self.session)
        self.assertEqual(set(value), {"session_id", "requested_at"})


if __name__ == "__main__":
    unittest.main()

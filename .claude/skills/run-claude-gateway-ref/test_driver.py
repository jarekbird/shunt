#!/usr/bin/env python3
"""Offline regression checks: no Docker, Claude or provider credentials."""

import contextlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse
import urllib.request

SPEC = importlib.util.spec_from_file_location("gwref_driver", Path(__file__).with_name("driver.py"))
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.probe = driver.Probe(self.temp.name)
        self.addCleanup(self.probe.close)

    def test_private_unique_captures_and_templates(self):
        other = driver.Probe(self.temp.name)
        self.addCleanup(other.close)
        self.assertNotEqual(self.probe.work, other.work)
        self.assertNotEqual(self.probe.run_id, other.run_id)
        self.probe.start_sink()
        self.probe.render()
        text = (self.probe.work / "gateway.yaml").read_text()
        self.assertNotIn("@@", text)
        for port in self.probe.ports.values():
            self.assertIn(str(port), text)
        self.assertNotIn("postgres@", text)
        for directory in (self.probe.work, self.probe.cap):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in self.probe.work.iterdir():
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_secret_file_symlink_is_refused(self):
        target = Path(self.temp.name) / "unrelated"
        target.write_text("keep")
        link = self.probe.cap / "token.json"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            driver.private_write(link, "overwrite")
        self.assertEqual(target.read_text(), "keep")

    def test_network_scope_and_redirects(self):
        with mock.patch.object(urllib.request.OpenerDirector, "open") as opened:
            for url in ("https://example.com", "http://127.0.0.1:1", self.probe.base + "@example.com/"):
                with self.assertRaises(driver.ProbeError):
                    self.probe.request(url)
            opened.assert_not_called()
        request = urllib.request.Request(self.probe.base)
        self.assertIsNone(driver.NoRedirect().redirect_request(request, None, 302, "", {}, "https://example.com"))

    def test_device_flow_refresh_and_exact_relay(self):
        probe = self.probe
        probe.start_sink()
        state = {"approved": False, "refreshed": False}

        class Gateway(http.server.BaseHTTPRequestHandler):
            def send(self, status=200, body=None, **headers):
                payload = json.dumps(body or {}).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def authorized(self):
                expected = "Bearer new-access" if state["refreshed"] else "Bearer old-access"
                return self.headers.get("Authorization") == expected

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/.well-known/oauth-authorization-server":
                    self.send(body={"device_authorization_endpoint": probe.base + "/device-auth",
                                    "token_endpoint": probe.base + "/oauth/token"})
                elif path == "/device":
                    self.send(**{"Set-Cookie": "gw_dev=fake-cookie; Path=/"})
                elif path == "/dex/auth":
                    self.send(302, Location="/dex/auth/local")
                elif path == "/dex/auth/local":
                    self.send(302, Location="/dex/login")
                elif path == "/dex/login":
                    self.send()
                elif path == "/oauth/callback":
                    state["approved"] = self.headers.get("Cookie") == "gw_dev=fake-cookie"
                    self.send()
                elif path == "/protocol":
                    self.send(body={"protocol": "fixture"})
                elif not self.authorized():
                    self.send(401)
                elif path == "/managed/settings":
                    if self.headers.get("If-None-Match") == '"fixture"':
                        self.send(304)
                    else:
                        self.send(body={"settings": {}}, ETag='"fixture"', **{"Set-Cookie": "fake-header-cookie=private"})
                elif path == "/v1/models":
                    self.send(body={"data": []})
                elif path == "/user/bootstrap":
                    self.send(404)
                else:
                    self.send(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                form = urllib.parse.parse_qs(body.decode())
                if self.path == "/device-auth":
                    self.send(body={"user_code": "local-test", "device_code": "fake-device"})
                elif self.path == "/device":
                    if self.headers.get("Origin") != probe.base or self.headers.get("Sec-Fetch-Site") != "same-origin":
                        self.send(400)
                    else:
                        self.send(302, Location=probe.dex + "/dex/auth")
                elif self.path == "/dex/login":
                    self.send(302, Location=probe.base + "/oauth/callback?code=fake")
                elif self.path == "/oauth/token":
                    if form.get("grant_type") == ["refresh_token"]:
                        state["refreshed"] = form.get("refresh_token") == ["old-refresh"]
                        self.send(body={"access_token": "new-access", "refresh_token": "new-refresh"})
                    elif state["approved"]:
                        self.send(body={"access_token": "old-access", "refresh_token": "old-refresh"})
                    else:
                        self.send(400, body={"error": "authorization_pending"})
                elif self.path == "/v1/metrics" and self.authorized():
                    url = "http://127.0.0.1:" + str(probe.ports["sink"])
                    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5) as response:
                        self.send(response.status)
                else:
                    self.send(401)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        probe.base = probe.dex = "http://127.0.0.1:" + str(server.server_port)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            probe.probe(*probe.login())
        self.assertTrue(state["approved"])
        self.assertTrue(state["refreshed"])
        self.assertEqual((probe.cap / "relay-body.json").read_bytes(), probe.metric)
        self.assertNotIn("old-access", output.getvalue())
        self.assertNotIn("new-refresh", output.getvalue())
        self.assertTrue((probe.cap / "bootstrap.json").is_file())
        self.assertNotIn("fake-header-cookie", (probe.cap / "managed_settings.json.headers.json").read_text())
        for path in probe.cap.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_cleanup_requires_label_and_removes_by_id(self):
        probe = self.probe
        probe.containers = ["unique-container"]
        for owner, should_remove in (("somebody-else", False), (probe.run_id, True)):
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                info = [{"Id": "exact-container-id", "Config": {"Labels": {driver.LABEL: owner}}}]
                return subprocess.CompletedProcess(args, 0, json.dumps(info).encode(), b"")

            with mock.patch.object(probe, "command", side_effect=command):
                if should_remove:
                    probe.close()
                else:
                    with self.assertRaises(driver.ProbeError):
                        probe.close()
            removes = [args for args in calls if args[1] == "rm"]
            self.assertEqual(removes, [["docker", "rm", "-f", "exact-container-id"]] if should_remove else [])
        probe.containers = []

    def test_command_deadline_captures_output_privately(self):
        with self.assertRaises(driver.ProbeError):
            self.probe.command([sys.executable, "-c", "import time; print('fake-private', flush=True); time.sleep(60)"], timeout=0.2)
        log = self.probe.work / "command-01.log"
        self.assertIn("fake-private", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    @unittest.skipUnless(shutil.which("lsof"), "reference driver requires lsof")
    def test_competing_listener_cannot_satisfy_gateway_readiness(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.probe.children.append(child)
        self.probe.ports["gateway"] = server.server_port
        with mock.patch.object(self.probe, "healthy", return_value=True) as health:
            self.assertFalse(self.probe.gateway_ready(child))
            health.assert_not_called()

    def test_container_create_is_unique_labelled_and_loopback_only(self):
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, ("a" * 64).encode(), b"")

        with mock.patch.object(self.probe, "command", side_effect=command):
            identity = self.probe.container("pg", "postgres:16-alpine", [])
        self.assertEqual(identity, "a" * 64)
        self.assertIn(driver.LABEL + "=" + self.probe.run_id, calls[0])
        self.assertIn("127.0.0.1:%d:5432" % self.probe.ports["pg"], calls[0])
        self.assertIn(self.probe.run_id, self.probe.containers[0])
        self.assertEqual(calls[1], ["docker", "start", identity])
        self.probe.containers = []

    def test_failure_signal_and_deadline_reap_owned_child(self):
        # Exercise the actual CLI finally block in a separate process, substituting
        # only service startup. No signal handlers in this test process are changed.
        for mode in ("failure", "signal", "deadline"):
            with self.subTest(mode=mode):
                pid_file = Path(self.temp.name) / (mode + ".pid")
                code = '''
import importlib.util, pathlib, subprocess, sys, time
spec=importlib.util.spec_from_file_location('probe',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.shutil.which=lambda name: '/offline-fixture'
mode=sys.argv[4]; pid_path=sys.argv[3]
def start(self):
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    self.children.append(child)
    pathlib.Path(pid_path).write_text(str(child.pid))
    if mode=='failure': raise m.ProbeError('fixture startup failure')
    if mode=='deadline': m.signal.setitimer(m.signal.ITIMER_REAL,0.2)
    time.sleep(60)
m.Probe.start=start
sys.argv=['driver.py','run','--output-root',sys.argv[2]]
raise SystemExit(m.main())
'''
                child = subprocess.Popen([sys.executable, "-B", "-c", code, str(Path(driver.__file__)),
                                          self.temp.name, str(pid_file), mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(driver.stop_child, child)
                until = time.monotonic() + 5
                while not pid_file.exists() and child.poll() is None and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                owned_pid = int(pid_file.read_text())
                if mode == "signal":
                    child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 1, (stdout, stderr))
                with self.assertRaises(ProcessLookupError):
                    os.kill(owned_pid, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Bounded, foreground reference probe; captures stay in a private run directory."""

import argparse
import base64
import contextlib
import http.cookiejar
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SKILL_DIR = Path(__file__).resolve().parent
LABEL = "dev.shunt.gateway-ref.run"


class ProbeError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_write(path, data):
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def stop_child(child):
    # Popen checks/reaps its own child before signalling; no saved PID is trusted.
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


class Probe:
    def __init__(self, output_root):
        root = Path(output_root).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="gwref-", dir=root))
        self.work.chmod(0o700)
        self.cap = self.work / "capture"
        self.cap.mkdir(mode=0o700)
        self.run_id = secrets.token_hex(12)
        self.containers = []
        self.children = []
        self.files = contextlib.ExitStack()
        self.reservations = {}
        self.command_count = 0
        self.sink = None
        self.sink_thread = None
        self.relayed = threading.Event()
        self.metric = json.dumps({"resourceMetrics": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "probe"}}]},
            "scopeMetrics": [{"scope": {"name": "probe"}, "metrics": [{
                "name": "claude_code.token.usage", "unit": "tokens", "sum": {
                    "dataPoints": [{"asInt": "42", "timeUnixNano": "1700000000000000000"}],
                    "aggregationTemporality": 2, "isMonotonic": True,
                },
            }]}],
        }]}).encode()
        self.openers = {}
        for role in ("gateway", "dex", "pg"):
            reservation = socket.socket()
            try:
                reservation.bind(("127.0.0.1", 0))
            except BaseException:
                reservation.close()
                raise
            self.reservations[role] = reservation
        self.ports = {k: s.getsockname()[1] for k, s in self.reservations.items()}
        self.base = "http://127.0.0.1:" + str(self.ports["gateway"])
        self.dex = "http://127.0.0.1:" + str(self.ports["dex"])

    def command(self, args, check=True, timeout=30):
        self.command_count += 1
        log = self.work / ("command-%02d.log" % self.command_count)
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            private_write(log, (exc.stdout or b"") + (exc.stderr or b""))
            raise ProbeError("Command timed out; see " + str(log)) from None
        private_write(log, result.stdout + result.stderr)
        if check and result.returncode:
            raise ProbeError("Command failed; see " + str(log))
        return result

    def request(self, url, *, method=None, form=None, body=None, headers=None,
                expected=(200,), jar="gateway"):
        origin = urllib.parse.urlsplit(url)
        if origin.scheme != "http" or origin.netloc not in {
            urllib.parse.urlsplit(self.base).netloc,
            urllib.parse.urlsplit(self.dex).netloc,
        } or origin.username or origin.password:
            raise ProbeError("Refused a request outside this run's loopback origins")
        if jar not in self.openers:
            self.openers[jar] = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect(),
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            )
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            response = self.openers[jar].open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProbeError("Loopback request failed") from None
        with response:
            data = response.read(4 * 1024 * 1024 + 1)
            status = response.code
            response_headers = {key.lower(): value for key, value in response.headers.items()}
        if len(data) > 4 * 1024 * 1024:
            raise ProbeError("Probe response exceeded 4 MiB")
        if status not in expected:
            raise ProbeError("Unexpected HTTP status: " + str(status))
        return status, response_headers, data

    def capture(self, name, url, **kwargs):
        status, headers, data = self.request(url, **kwargs)
        private_write(self.cap / name, data)
        shareable_headers = {key: value for key, value in headers.items()
                             if key not in {"set-cookie", "cookie", "authorization", "proxy-authorization"}}
        private_write(self.cap / (name + ".headers.json"), json.dumps(shareable_headers, indent=2))
        return status, headers, data

    def container(self, role, image, args):
        name = "shunt-gw-" + role + "-" + self.run_id
        # Record before create: an interrupted create can still leave a resource.
        self.containers.append(name)
        created = self.command([
            "docker", "create", "--name", name, "--label", LABEL + "=" + self.run_id,
            "-p", "127.0.0.1:%d:%d" % (self.ports[role], 5432 if role == "pg" else 5556),
            *args, image, *(["dex", "serve", "/etc/dex/config.yaml"] if role == "dex" else []),
        ], timeout=90)
        container_id = created.stdout.decode().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ProbeError("Docker create returned no container ID")
        self.reservations.pop(role).close()
        self.command(["docker", "start", container_id])
        return container_id

    def start_sink(self):
        probe = self

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if not 0 <= length <= 1024 * 1024:
                    self.send_error(413)
                    return
                self.connection.settimeout(5)
                body = self.rfile.read(length)
                private_write(probe.cap / "relay-body.json", body)
                if body == probe.metric:
                    probe.relayed.set()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
        self.ports["sink"] = self.sink.server_port
        self.sink_thread = threading.Thread(target=self.sink.serve_forever, daemon=True)
        self.sink_thread.start()

    def render(self):
        values = {name.upper() + "_PORT": str(port) for name, port in self.ports.items()}
        values["JWT_SECRET"] = base64.b64encode(secrets.token_bytes(32)).decode()
        values["PG_PASSWORD"] = secrets.token_hex(24)
        private_write(self.work / "postgres.env", "POSTGRES_PASSWORD=" + values["PG_PASSWORD"] + "\n")
        for name in ("gateway.yaml", "dex.yaml"):
            text = (SKILL_DIR / name).read_text()
            for key, value in values.items():
                text = text.replace("@@" + key + "@@", value)
            if re.search(r"@@[A-Z_]+@@", text):
                raise ProbeError("Unresolved config template field")
            private_write(self.work / name, text)

    def wait_ready(self, check, child=None):
        until = time.monotonic() + 30
        while time.monotonic() < until:
            if child is not None and child.poll() is not None:
                raise ProbeError("Gateway exited during startup; inspect the private gateway.log")
            if check():
                return
            time.sleep(0.25)
        raise ProbeError("Service did not become ready within 30 seconds")

    def healthy(self, url):
        try:
            return self.request(url)[0] == 200
        except ProbeError:
            return False

    def gateway_ready(self, child):
        # A competing listener must not turn a failed child into a green run.
        owned = self.command([
            "lsof", "-nP", "-a", "-p", str(child.pid),
            "-iTCP:" + str(self.ports["gateway"]), "-sTCP:LISTEN", "-t",
        ], check=False, timeout=5)
        return str(child.pid) in owned.stdout.decode().split() and self.healthy(self.base + "/healthz")

    def start(self):
        version = self.command(["claude", "--version"], timeout=10)
        private_write(self.work / "reference-version.txt", version.stdout)
        self.start_sink()
        self.render()
        private_write(self.work / "run.json", json.dumps({"run_id": self.run_id, "ports": self.ports}, indent=2))
        pg = self.container("pg", "postgres:16-alpine", ["--env-file", str(self.work / "postgres.env")])
        self.container("dex", "ghcr.io/dexidp/dex:v2.44.0", [
            "-v", str(self.work / "dex.yaml") + ":/etc/dex/config.yaml:ro",
        ])
        self.wait_ready(lambda: self.command([
            "docker", "exec", pg, "pg_isready", "-U", "postgres", "-q",
        ], check=False, timeout=5).returncode == 0)
        self.wait_ready(lambda: self.healthy(self.dex + "/dex/.well-known/openid-configuration"))
        self.reservations.pop("gateway").close()
        log_path = self.work / "gateway.log"
        private_write(log_path, b"")
        log = self.files.enter_context(log_path.open("ab"))
        config_home = self.work / "claude-home"
        config_home.mkdir(mode=0o700)
        child = subprocess.Popen([
            "claude", "gateway", "--config", str(self.work / "gateway.yaml"),
        ], stdout=log, stderr=subprocess.STDOUT, env={
            **os.environ, "CLAUDE_CONFIG_DIR": str(config_home),
            "CLAUDE_GATEWAY_ALLOW_LOOPBACK": "1",
        })
        self.children.append(child)
        self.wait_ready(lambda: self.gateway_ready(child), child)

    def login(self):
        _, _, data = self.capture("discovery.json", self.base + "/.well-known/oauth-authorization-server")
        discovery = json.loads(data)
        endpoint = discovery["device_authorization_endpoint"]
        token_endpoint = discovery["token_endpoint"]
        _, _, data = self.capture("device_auth.json", endpoint, form={"client_id": "claude-code"})
        device = json.loads(data)
        device_url = self.base + "/device?" + urllib.parse.urlencode({"user_code": device["user_code"]})
        self.request(device_url)
        _, headers, _ = self.request(self.base + "/device", form={"user_code": device["user_code"]},
                                      headers={"Origin": self.base, "Referer": device_url,
                                               "Sec-Fetch-Site": "same-origin"}, expected=(302, 303))
        url = urllib.parse.urljoin(self.base, headers["location"])
        # Three explicit hops; no automatic or off-origin redirects are followed.
        for _ in range(2):
            _, headers, _ = self.request(url, jar="dex", expected=(302, 303))
            url = urllib.parse.urljoin(url, headers["location"])
        self.request(url, jar="dex")
        _, headers, _ = self.request(url, jar="dex", form={"login": "dev@example.com", "password": "password"},
                                      expected=(302, 303))
        callback = urllib.parse.urljoin(url, headers["location"])
        if not callback.startswith(self.base + "/oauth/callback?"):
            raise ProbeError("Unexpected gateway callback")
        self.request(callback, expected=(200, 302, 303))
        for _ in range(12):
            _, _, data = self.capture("token.json", token_endpoint, expected=(200, 400), form={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device["device_code"], "client_id": "claude-code",
            })
            token = json.loads(data)
            if "access_token" in token and "refresh_token" in token:
                return token, token_endpoint
            if token.get("error") != "authorization_pending":
                raise ProbeError("Device authorization failed; inspect private captures")
            time.sleep(5)
        raise ProbeError("Device authorization did not complete")

    def probe(self, token, token_endpoint):
        headers = {"Authorization": "Bearer " + token["access_token"]}
        self.capture("protocol.md", self.base + "/protocol")
        _, settings_headers, data = self.capture("managed_settings.json", self.base + "/managed/settings", headers=headers)
        json.loads(data)
        etag = next(value for key, value in settings_headers.items() if key.lower() == "etag")
        self.request(self.base + "/managed/settings", headers={**headers, "If-None-Match": etag}, expected=(304,))
        _, _, data = self.capture("models.json", self.base + "/v1/models", headers=headers)
        if not isinstance(json.loads(data)["data"], list):
            raise ProbeError("Model response omitted its data list")
        _, _, data = self.capture("token_refresh.json", token_endpoint, form={
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": "claude-code",
        })
        refreshed = json.loads(data)
        if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
            raise ProbeError("Refresh response omitted tokens")
        if refreshed["refresh_token"] == token["refresh_token"]:
            raise ProbeError("Reference refresh token did not rotate; inspect private captures")
        headers = {"Authorization": "Bearer " + refreshed["access_token"]}
        self.request(self.base + "/v1/metrics", body=self.metric,
                     headers={**headers, "Content-Type": "application/json"})
        if not self.relayed.wait(timeout=5):
            raise ProbeError("Telemetry did not arrive byte-identical at this run's sink")
        self.capture("bootstrap.json", self.base + "/user/bootstrap", headers=headers, expected=(404,))

    def close(self):
        errors = []
        for child in reversed(self.children):
            try:
                stop_child(child)
            except (OSError, subprocess.TimeoutExpired):
                errors.append("could not reap the owned gateway child")
        self.files.close()
        if self.sink is not None:
            self.sink.shutdown()
            self.sink.server_close()
            self.sink_thread.join(timeout=5)
        for reservation in self.reservations.values():
            reservation.close()
        for name in reversed(self.containers):
            try:
                result = self.command(["docker", "inspect", "--type", "container", name], check=False, timeout=10)
                if result.returncode and b"No such" in result.stderr:
                    continue
                if result.returncode:
                    raise ProbeError("Docker inspection failed")
                info = json.loads(result.stdout)[0]
                if info.get("Config", {}).get("Labels", {}).get(LABEL) != self.run_id:
                    raise ProbeError("Container ownership label did not match")
                self.command(["docker", "rm", "-f", info["Id"]], timeout=10)
            except (ProbeError, ValueError, KeyError, IndexError):
                errors.append("cleanup incomplete for " + name)
        if errors:
            raise ProbeError("; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"], help="start, capture, and clean up in one foreground run")
    parser.add_argument("--output-root", default=os.environ.get("GWREF_WORK"), help="capture parent directory; or set GWREF_WORK")
    parser.add_argument("--timeout", type=int, default=180, help="whole-run deadline in seconds (30..1800; default 180)")
    args = parser.parse_args()
    if not args.output_root:
        parser.error("--output-root or GWREF_WORK is required; use the host's artifact volume")
    if not 30 <= args.timeout <= 1800:
        parser.error("--timeout must be between 30 and 1800")
    missing = [name for name in ("docker", "claude", "lsof") if not shutil.which(name)]
    if missing:
        parser.error("missing prerequisites: " + ", ".join(missing))
    os.umask(0o077)
    probe = Probe(args.output_root)

    def interrupted(signum, frame):
        raise ProbeError("Probe interrupted or timed out; cleaning up this run")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, interrupted)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    result = 0
    print("Private run directory: " + str(probe.work), flush=True)
    try:
        probe.start()
        probe.probe(*probe.login())
        print("Reference captures complete.")
    except ProbeError as exc:
        print(str(exc), file=os.sys.stderr)
        result = 1
    except (ValueError, KeyError, StopIteration, OSError):
        print("Reference probe failed; inspect the private run directory.", file=os.sys.stderr)
        result = 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        # A second signal cannot interrupt ownership-checked, bounded cleanup.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            probe.close()
        except ProbeError as exc:
            print(str(exc), file=os.sys.stderr)
            result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())

"""Exercise console HTTP handling in memory and discovery with a local stdio peer."""

from contextlib import redirect_stderr
from http.client import HTTPResponse
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from src.wyrit.configured_host import configured_host
from src.wyrit.console.__main__ import ConsoleServer
from src.wyrit.host_config import load_config

from test_live_sessions import FIXTURE


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "wyrit.toml"
        self.path.write_text("# Operator comment\n[servers]\n", encoding="utf-8")
        self.host = self.enterContext(configured_host(self.path))
        with patch.object(ConsoleServer, "server_bind"), patch.object(ConsoleServer, "server_activate"):
            self.server = self.enterContext(ConsoleServer(self.path, self.host, 4790))
        self.assertEqual(self.server.server_address, ("127.0.0.1", 4790))
        self.server.server_port = 4790

    def request(self, path, payload=None, *, host=None, token=None, method=None):
        method = method or ("POST" if payload is not None else "GET")
        headers = [f"{method} {path} HTTP/1.1", f"Host: {host or '127.0.0.1:4790'}"]
        if token is not None:
            headers.append(f"X-Wyrit-Token: {token}")
        body = json.dumps(payload).encode() if payload is not None else b""
        if body:
            headers.extend(("Content-Type: application/json", f"Content-Length: {len(body)}"))
        wire = "\r\n".join([*headers, "", ""]).encode() + body
        socket = MemorySocket(wire)
        self.server.finish_request(socket, ("127.0.0.1", 60000))
        with HTTPResponse(MemorySocket(socket.output.getvalue())) as response:
            response.begin()
            content = response.read()
            csp = response.getheader("Content-Security-Policy")
            self.assertIn("default-src 'none'", csp)
            self.assertIn("font-src 'self'", csp)
            self.assertIn("connect-src 'self'", csp)
            self.assertIn("script-src 'nonce-", csp)
            self.assertIn("style-src 'nonce-", csp)
            if response.getheader("Content-Type").startswith("application/json"):
                self.assertNotIn(self.server.change_token.encode(), content)
            return response.status, content, csp

    def change(self, action, **payload):
        return self.request("/api/config", {"action": action, **payload}, token=self.server.change_token)

    def test_host_headers_and_change_token(self):
        for host in ("example.org", "localhost", "127.0.0.1:1"):
            self.assertEqual(self.request("/", host=host)[0], 403)
        self.assertEqual(self.request("/api/config", {"action": "reload"}, host="example.org",
                                      token=self.server.change_token)[0], 403)
        self.assertEqual(self.request("/", host="example.org", method="PUT")[0], 403)
        for token in (None, "incorrect"):
            self.assertEqual(self.request("/api/config", {"action": "reload"}, token=token)[0], 403)
        self.assertEqual(self.change("reload")[0], 200)

    def test_pages_and_font(self):
        for path, page in (("/", "servers"), ("/servers", "servers"),
                           ("/capabilities", "capabilities"), ("/policy", "policy")):
            status, content, csp = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(f'<body data-page="{page}">'.encode(), content)
            self.assertIn(self.server.change_token.encode(), content)
            nonce = re.search(r"script-src 'nonce-([^']+)'", csp)[1]
            self.assertEqual(content.count(f'nonce="{nonce}"'.encode()), 2)
        self.assertTrue(self.request("/assets/jetbrains-mono-latin.woff2")[1].startswith(b"wOF2"))
        self.assertEqual(self.request("/missing")[0], 404)

    def test_probe_save_exposure_policy_limits_disable_remove(self):
        fixture = self.directory / "peer.py"
        fixture.write_text(FIXTURE, encoding="utf-8")
        settings = {
            "namespace": "peer", "transport": "stdio", "expose": "all",
            "command": sys.executable,
            "args": [str(fixture), str(self.directory / "peer.pid"), str(self.directory / "calls")],
        }
        original = self.path.read_bytes()
        self.assertEqual(self.change("test", name="peer", settings=settings)[0], 200)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.host.health, {})

        self.assertEqual(self.change("save", name="peer", settings=settings)[0], 200)
        handwritten = self.directory / "manual.toml"
        handwritten.write_text(
            '[servers.peer]\nnamespace = "peer"\ntransport = "stdio"\nexpose = "all"\n'
            f'command = {json.dumps(settings["command"])}\nargs = {json.dumps(settings["args"])}\n',
            encoding="utf-8",
        )
        self.assertEqual(tomllib.loads(self.path.read_text()), tomllib.loads(handwritten.read_text()))
        self.assertEqual(load_config(self.path), load_config(handwritten))
        self.assertEqual(self.path.with_suffix(".toml.bak").read_bytes(), original)
        self.assertIn("peer.original", self.host.ids)

        self.assertEqual(self.change("policy", name="peer", tool="original", deny_writes=False)[0], 200)
        self.assertEqual(self.change("exposure", name="peer", tool="original", exposed=False)[0], 200)
        self.assertNotIn("peer.original", self.host.ids)
        server = json.loads(self.request("/api/state")[1])["servers"][0]
        tool = next(item for item in server["tools"] if item["name"] == "original")
        self.assertFalse(tool["exposed"])
        self.assertEqual(tool["classification"], "read")
        self.assertEqual(tool["input_schema"], {"type": "object"})
        self.assertFalse(server["settings"]["capabilities"]["original"]["deny_writes"])
        self.assertEqual(self.change("exposure", name="peer", tool="original", exposed=True)[0], 200)
        self.assertIn("peer.original", self.host.ids)

        self.assertEqual(self.change("limits", name="peer", limits={"max_calls": 7})[0], 200)
        limits = json.loads(self.request("/api/state")[1])["limits"]
        self.assertEqual(limits["servers"]["peer"]["max_calls"], {"value": 7, "source": "server"})
        before = self.path.read_bytes()
        backup = self.path.with_suffix(".toml.bak").read_bytes()
        health = self.host.health
        status, body, _ = self.change("limits", name="peer", limits={"max_calls": 0})
        self.assertEqual(status, 400)
        self.assertIn(b"positive int limit", body)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.with_suffix(".toml.bak").read_bytes(), backup)
        self.assertEqual(self.host.health, health)
        self.assertIn("peer.original", self.host.ids)

        self.assertEqual(self.change("enabled", name="peer", enabled=False)[0], 200)
        self.assertEqual(self.host.ids, ())
        self.assertEqual(json.loads(self.request("/api/state")[1])["servers"][0]["health"], "disabled")
        self.assertEqual(self.change("remove", name="peer")[0], 200)

    def test_header_secret_remains_in_environment(self):
        secret = "ghp_" + "x" * 36
        settings = {
            "namespace": "remote", "transport": "http", "expose": "all", "enabled": False,
            "url": "https://peer.invalid/mcp",
            "headers": {"Authorization": {"env": "CONSOLE_TEST_SECRET", "prefix": "Bearer "}},
        }
        logs = StringIO()
        with patch.dict(os.environ, {"CONSOLE_TEST_SECRET": secret}), redirect_stderr(logs):
            status, saved, _ = self.change("save", name="remote", settings=settings)
            self.assertEqual(status, 200)
            status, state, _ = self.request("/api/state")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(state)["servers"][0]["headers"]["Authorization"])
            injected = settings | {"url": settings["url"] + "/" + secret}
            status, error, _ = self.change("save", name="remote", previous="remote", settings=injected)
            self.assertEqual(status, 400)
        for content in (saved, state, error, self.path.read_bytes(), logs.getvalue().encode()):
            self.assertNotIn(secret.encode(), content)

    def test_hand_edit_reload(self):
        revision = json.loads(self.request("/api/state")[1])["revision"]
        self.path.write_text("[limits]\nmax_calls = 5\n", encoding="utf-8")
        self.assertEqual(self.change("limits", revision=revision, limits={"max_calls": 10})[0], 400)
        self.assertEqual(self.change("reload")[0], 200)
        _, _, budgets, _ = self.host.bind_run(include_settings=True)
        self.assertEqual(budgets.effective()["max_calls"].value, 5)
        self.assertFalse(self.path.with_suffix(".toml.bak").exists())

    def test_disabled_server_can_show_missing_header(self):
        settings = {
            "namespace": "remote", "transport": "http", "expose": "all", "enabled": False,
            "url": "https://peer.invalid/mcp",
            "headers": {"Authorization": {"env": "CONSOLE_TEST_MISSING"}},
        }
        with patch.dict(os.environ, clear=True):
            self.assertEqual(self.change("save", name="remote", settings=settings)[0], 200)
            status, state, _ = self.request("/api/state")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(state)["servers"][0]["headers"]["Authorization"])
            self.assertEqual(self.change("enabled", name="remote", enabled=True)[0], 400)
            self.assertEqual(self.change("test", name="remote", previous="remote", settings=settings)[0], 400)


class MemorySocket:
    def __init__(self, incoming):
        self.incoming = incoming
        self.output = BytesIO()

    def makefile(self, mode, *args):
        return BytesIO(self.incoming)

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, seconds):
        pass

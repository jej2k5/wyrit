import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from anyio import run
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.wyrit.configured_host import configured_capabilities
from src.wyrit.host_config import ConfigError, load_config
from src.wyrit.invocation import CallError
from src.wyrit.mcp_server import main


SECRET = "gh" + "p_" + "x" * 36
SOURCE = Path(__file__).resolve().parents[1] / "src"
STDIO = """
[servers.local]
namespace = "fixture"
transport = "stdio"
expose = "all"
"""
HTTP = """
[servers.remote]
namespace = "remote"
transport = "http"
expose = "all"
"""
HEADER = """
[servers.remote.headers]
Authorization = { env = "WYRIT_TEST_SECRET", prefix = "Bearer " }
"""
TOOLS = [{"name": name, "inputSchema": {"type": "object"}} for name in ("listed", "hidden")]
INITIALIZED = {"capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}


class HostConfigTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "wyrit.toml"
        self.env = {**os.environ, "PYTHONPATH": str(SOURCE), "WYRIT_TEST_SECRET": SECRET}
        self.enterContext(patch.dict(os.environ, {"WYRIT_TEST_SECRET": SECRET}))

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "wyrit.mcp_server", *args],
            input="", capture_output=True, text=True, env=self.env, timeout=15,
        )

    def test_requires_startup_mode_and_accepts_demo(self):
        missing = self.cli()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--config", missing.stderr)
        self.assertEqual(self.cli("--demo").returncode, 0)

    def test_validation_errors_refuse_startup(self):
        cases = (
            (STDIO + 'command = "python"\nextra = true\n', "local", "extra", "unknown"),
            (STDIO + 'command = "python"\n' + HTTP.replace('namespace = "remote"', 'namespace = "fixture"')
             + 'url = "https://example.invalid/mcp"\n', "remote", "namespace", "duplicate"),
            (HTTP + 'url = "https://example.invalid/mcp"\n' + HEADER.replace(
                "WYRIT_TEST_SECRET", "WYRIT_PHASE8_UNSET"), "remote", "headers.Authorization.env", "missing"),
            (HTTP + 'url = "http://localhost:bad/mcp"\n', "remote", "url", "valid HTTP"),
            (STDIO + 'command = "  "\n', "local", "command", "nonempty"),
            (HTTP + 'url = "https://example.invalid/mcp"\n[servers.remote.headers]\n'
             'Authorization = "inline"\n', "remote", "headers.Authorization", "environment reference"),
        )
        for content, name, field, reason in cases:
            with self.subTest(field=field):
                self.path.write_text(content, encoding="utf-8")
                result = self.cli("--config", str(self.path))
                self.assertNotEqual(result.returncode, 0)
                for fragment in (name, field, reason):
                    self.assertIn(fragment, result.stderr)
                self.assertNotIn(SECRET, result.stderr + result.stdout)

    def test_credential_shaped_literal_is_rejected_without_echo(self):
        content = HTTP + 'url = "https://example.invalid/mcp"\n[servers.remote.headers]\n'
        with patch.object(Path, "read_text", return_value=content + f'Authorization = "{SECRET}"\n'):
            with self.assertRaisesRegex(ConfigError, "headers.Authorization") as error:
                load_config(self.path)
        self.assertNotIn(SECRET, str(error.exception))

    def test_stdio_search_respects_exposure(self):
        fixture = self.path.with_name("fake_stdio.py")
        fixture.write_text(
            "import json, sys\n"
            f"tools = {TOOLS!r}\n"
            f"initialized = {INITIALIZED!r}\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if 'id' not in message:\n"
            "        continue\n"
            "    if message['method'] == 'initialize':\n"
            "        result = dict(initialized, protocolVersion=message['params']['protocolVersion'])\n"
            "    else:\n"
            "        result = {'tools': tools}\n"
            "    print(json.dumps(dict(jsonrpc='2.0', id=message['id'], result=result)), flush=True)\n",
            encoding="utf-8",
        )
        for exposure, expected in (('"all"', {"fixture.listed", "fixture.hidden"}),
                                   ('["listed"]', {"fixture.listed"})):
            with self.subTest(exposure=exposure):
                self.path.write_text(
                    STDIO.replace('"all"', exposure) + f"command = {json.dumps(sys.executable)}\n"
                    f"args = {json.dumps([str(fixture)])}\n", encoding="utf-8",
                )

                async def search():
                    params = StdioServerParameters(
                        command=sys.executable, env=self.env,
                        args=["-m", "wyrit.mcp_server", "--config", str(self.path)],
                    )
                    async with stdio_client(params) as streams, ClientSession(*streams) as session:
                        await session.initialize()
                        reply = await session.call_tool("search", {"query": "fixture"})
                        return json.loads(reply.content[0].text)

                self.assertEqual({match["id"] for match in run(search)["matches"]}, expected)

    def test_http_credentials_stay_in_host_and_redirects_are_not_followed(self):
        requests = []

        def downstream(http_request):
            path = http_request.url.path
            request = json.loads(http_request.content)
            requests.append((path, request["method"], http_request.headers.get("Authorization")))
            if path == "/redirect":
                return httpx.Response(307, headers={"Location": "/mcp"})
            if path == "/error":
                reply = {"error": {"code": -32603, "message": SECRET}}
            elif request["method"] == "initialize":
                reply = {"result": dict(INITIALIZED, protocolVersion=request["params"]["protocolVersion"])}
            elif "id" not in request:
                return httpx.Response(202)
            elif request["method"] == "tools/list":
                reply = {"result": {"tools": TOOLS}}
            else:
                reply = {"result": {"content": [{"type": "text", "text": SECRET}]}}
            return httpx.Response(200, json=dict(reply, jsonrpc="2.0", id=request["id"]))

        client = httpx.AsyncClient
        self.enterContext(patch.object(
            httpx, "AsyncClient",
            side_effect=lambda **kwargs: client(transport=httpx.MockTransport(downstream), **kwargs),
        ))

        def config_at(path):
            self.path.write_text(
                HTTP + f'url = "https://example.invalid/{path}"\n' + HEADER,
                encoding="utf-8",
            )
            return load_config(self.path)

        config = config_at("mcp")
        self.assertNotIn(SECRET, repr(config))
        with configured_capabilities(config) as (registry, dispatch):
            self.assertEqual(set(registry.ids), {"remote.listed", "remote.hidden"})
            with self.assertRaises(CallError) as failure:
                dispatch("remote.listed", {}, deadline=time.monotonic() + 5)
            self.assertEqual(failure.exception.code, "INVALID_RESULT")
            self.assertNotIn(SECRET, str(failure.exception))
        self.assertIn(("/mcp", "tools/call", "Bearer " + SECRET), requests)

        for path in ("error", "redirect"):
            with self.subTest(path=path):
                requests.clear()
                config_at(path)
                stderr = StringIO()
                with patch.object(sys, "argv", ["wyrit", "--config", str(self.path)]), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as result:
                        main()
                self.assertNotEqual(result.exception.code, 0)
                self.assertNotIn(SECRET, stderr.getvalue())
                self.assertEqual(requests, [(f"/{path}", "initialize", "Bearer " + SECRET)])

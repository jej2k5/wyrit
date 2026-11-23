"""Run the threaded console on the IPv4 loopback interface."""

import argparse
from hashlib import sha256
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
from pathlib import Path
import secrets
from sqlite3 import Error as StorageError
from urllib.parse import parse_qs

from ..capture import DEFAULT_SOURCE_SECONDS, CaptureStore, contains_credential
from ..configured_host import configured_host
from ..host_config import ConfigError, load_config
from ..mcp_adapter import _contains_secret
from .configuration import Configuration
from .monitoring import Monitoring, WINDOWS


PAGES = {"/": "servers", "/servers": "servers", "/capabilities": "capabilities",
         "/policy": "policy", "/monitor": "monitor"}
ASSETS = files(__package__).joinpath("assets")
FONT = "/assets/jetbrains-mono-latin.woff2"
MAX_BODY = 256 * 1024


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, path, host, port):
        self.change_token = secrets.token_urlsafe(32)
        self.configuration = Configuration(path, host, self.change_token)
        self.monitor = None
        super().__init__(("127.0.0.1", port), ConsoleHandler)

    def server_close(self):
        super().server_close()
        if self.monitor is not None:
            self.monitor.close()

    def monitoring(self):
        path = self.configuration.host.ledger_path
        if self.monitor is None or self.monitor.path != path:
            if self.monitor is not None:
                self.monitor.close()
            self.monitor = Monitoring(path)
        return self.monitor

    def capture_servers(self):
        config = load_config(self.configuration.path)
        self.configuration.secrets.update(config.secrets)
        return [(server.name, server.namespace) for server in config.servers]

    def handle_error(self, request, address):
        # Tracebacks can contain downstream data or raw operator input.
        pass


class ConsoleHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(20)
        super().setup()

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": "Use a supported console request."})

    def parse_request(self):
        return super().parse_request() and self.valid_host()

    def reply(self, status, content, media="application/json; charset=utf-8", nonce=None):
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        nonce = nonce or secrets.token_urlsafe(24)
        if isinstance(content, dict):
            if (contains_credential(content)
                    or _contains_secret(content, self.server.configuration.secrets.copy())):
                status, content = 400, {"error": "Remove sensitive data from the configuration or downstream metadata."}
            content = json.dumps(content).encode()
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                         "font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(content)

    def valid_host(self):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}"}
        if port == 80:
            allowed.add("127.0.0.1")
        if len(self.headers.get_all("Host", [])) != 1 or self.headers["Host"] not in allowed:
            self.reply(403, {"error": f"Open the console at http://127.0.0.1:{port}."})
            return False
        return True

    def do_GET(self):
        if self.path in PAGES:
            nonce = secrets.token_urlsafe(24)
            def asset(name):
                return ASSETS.joinpath(name).read_text(encoding="utf-8")

            html = (asset("index.html").replace("@PAGE@", PAGES[self.path])
                    .replace("@NONCE@", nonce).replace("@TOKEN@", self.server.change_token)
                    .replace("@STYLE@", asset("console.css")).replace("@SCRIPT@", asset("console.js")))
            self.reply(200, html.encode(), "text/html; charset=utf-8", nonce)
        elif self.path == FONT:
            self.reply(200, ASSETS.joinpath(FONT.rsplit("/", 1)[1]).read_bytes(), "font/woff2")
        elif self.path == "/api/state":
            self.run_configuration(self.server.configuration.state)
        else:
            path, _, query = self.path.partition("?")
            if path in {"/api/monitor", "/api/source", "/api/captures"}:
                self.run_configuration(lambda: self.monitor_request(path, parse_qs(query, max_num_fields=8)))
            else:
                self.reply(404, {"error": "Open Servers, Capabilities, Policy and limits or Monitoring."})

    def monitor_request(self, path, query):
        monitor = self.server.monitoring()
        host = self.server.configuration.host
        store = CaptureStore(monitor.path)
        if path in {"/api/source", "/api/captures"}:
            ref, = query.get("ref", [""])
            if len(ref) > 20 or ref[:1] not in {"r", "h"} or not ref[1:].isascii() or not ref[1:].isdigit():
                raise ConfigError("Choose a recorded source.")
            if query.get("epoch") != [monitor.epoch]:
                raise ConfigError("Refresh Monitoring to restore source links.")
            record = monitor.source(ref)
            if record is None:
                raise ConfigError("Choose a recorded source.")
            if path == "/api/source":
                return record
            if not ref.startswith("r"):
                raise ConfigError("Choose a run to view its captured payloads.")
            servers = self.server.capture_servers()
            run_id = record["record"]["run_id"]
            return {"payloads": store.payloads(run_id, servers), "source": store.sources([run_id]).get(run_id)}
        window, = query.get("window", ["hour"])
        cursor, = query.get("after", ["0"])
        epoch, = query.get("epoch", [""])
        if window not in WINDOWS or not 0 <= int(cursor):
            raise ConfigError("Choose an hour, day or week window.")
        snapshot = monitor.snapshot(host.monitor_id, window, int(cursor), epoch,
                                    paused=query.get("paused") == ["1"])
        policies = sorted(store.source_policies().items())
        payload_policies = sorted(store.policies().items())
        revision = sha256(repr((policies, payload_policies)).encode()).hexdigest()
        start, = query.get("start", ["0"])
        if epoch == monitor.epoch and query.get("source_revision") != [revision]:
            rows = monitor.runs.rows[int(start):] if 0 <= int(start) <= snapshot["cursor"] else []
        else:
            rows = snapshot["feed"]
        previews = store.sources([row["record"]["run_id"] for row in rows], preview=True)
        snapshot["previews"] = {row["ref"]: previews[row["record"]["run_id"]]
                                for row in rows if row["record"]["run_id"] in previews}
        snapshot["source_revision"] = revision
        snapshot["capture_enabled"] = {
            "source_overrides": sum(seconds != DEFAULT_SOURCE_SECONDS for _, (seconds, _) in policies),
            "payload_servers": len(payload_policies),
        }
        snapshot["health_record_error"] = host.health_record_error
        return snapshot

    def do_POST(self):
        if self.path != "/api/config":
            self.reply(404, {"error": "Use the configuration endpoint."})
            return
        tokens = self.headers.get_all("X-Wyrit-Token", [])
        if len(tokens) != 1 or not compare_digest(tokens[0].encode(), self.server.change_token.encode()):
            self.reply(403, {"error": "Refresh the console and try again."})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding")
                    or len(lengths) != 1 or not 0 < int(lengths[0]) <= MAX_BODY):
                raise ValueError
            request = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(request, dict):
                raise ValueError
        except (ValueError, RecursionError, TimeoutError):
            self.reply(400, {"error": "Send a JSON object within the request size limit."})
            return
        self.run_configuration(lambda: self.apply(request))

    def apply(self, request):
        if request.get("action") == "capture_status":
            servers = self.server.capture_servers()
            store = CaptureStore(self.server.configuration.host.ledger_path)
            settings = store.settings()
            sources = store.source_policies()
            return {"servers": [{"name": key[0], "namespace": key[1], "seconds": settings.get(key, 0),
                                 "source_seconds": sources.get(key, (DEFAULT_SOURCE_SECONDS, None))[0]}
                                for key in servers]}
        if request.get("action") in {"capture", "capture_source"}:
            key = request.get("name"), request.get("namespace")
            if key not in self.server.capture_servers():
                raise ConfigError("Select a configured server.")
            store = CaptureStore(self.server.configuration.host.ledger_path)
            if request["action"] == "capture_source":
                store.configure_source(key, request.get("seconds"))
            else:
                store.configure(key, request.get("seconds"))
            return {"message": "Capture settings saved."}
        return self.server.configuration.apply(request)

    def run_configuration(self, operation):
        with self.server.configuration.lock:
            try:
                result = operation()
            except ConfigError as error:
                self.reply(400, {"error": str(error)})
            except (TypeError, ValueError, AttributeError, KeyError, OverflowError, RecursionError):
                self.reply(400, {"error": "Check the configuration fields and try again."})
            except (OSError, RuntimeError, StorageError):
                self.reply(500, {"error": "Check that the config file and its directory are accessible, then reload."})
            else:
                self.reply(200, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=4790)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # The downstream SDK can log URLs, protocol payloads and transport exceptions.
    logging.disable(logging.CRITICAL)
    try:
        path = args.config.resolve()
        with configured_host(path) as host, ConsoleServer(path, host, args.port) as server:
            print(f"Wyrit console at http://127.0.0.1:{args.port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except ConfigError:
        parser.exit(1, "Check the TOML configuration and required environment variables.\n")
    except OSError:
        parser.exit(1, "Could not start the console. Check the port and configuration path.\n")


if __name__ == "__main__":
    main()

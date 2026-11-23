"""Run the threaded console on the IPv4 loopback interface."""

import argparse
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
from pathlib import Path
import secrets

from ..configured_host import configured_host
from ..host_config import ConfigError
from ..mcp_adapter import _contains_secret
from .configuration import Configuration


PAGES = {"/": "servers", "/servers": "servers", "/capabilities": "capabilities", "/policy": "policy"}
ASSETS = files(__package__).joinpath("assets")
FONT = "/assets/jetbrains-mono-latin.woff2"
MAX_BODY = 256 * 1024


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, path, host, port):
        self.change_token = secrets.token_urlsafe(32)
        self.configuration = Configuration(path, host, self.change_token)
        super().__init__(("127.0.0.1", port), ConsoleHandler)

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
            if _contains_secret(content, self.server.configuration.secrets.copy()):
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
            self.reply(404, {"error": "Open Servers, Capabilities or Policy and limits."})

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
        self.run_configuration(lambda: self.server.configuration.apply(request))

    def run_configuration(self, operation):
        with self.server.configuration.lock:
            try:
                result = operation()
            except ConfigError as error:
                self.reply(400, {"error": str(error)})
            except (TypeError, ValueError, AttributeError, KeyError, OverflowError, RecursionError):
                self.reply(400, {"error": "Check the configuration fields and try again."})
            except (OSError, RuntimeError):
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

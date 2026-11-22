"""Disposable JavaScript execution with a run-scoped fake echo capability."""

from dataclasses import dataclass
from pathlib import Path
import json
import math
import os
import selectors
import shutil
import subprocess
import time

from .capabilities import RunAuthorization


MESSAGE_BYTES = 64 * 1024
RESULT_BYTES = 16 * 1024
STDOUT_BYTES = 256 * 1024
STDERR_BYTES = 8 * 1024
MAX_CALLS = 32
ECHO_CAPABILITY = "fake.echo"


@dataclass(frozen=True)
class RunResult:
    status: str
    value: object = None
    reason: str | None = None


class _RunFailure(Exception):
    pass


def _encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return value


def run_javascript(source, *, run_id, authorization, timeout=2.0):
    """Run an async function body once. The only fake endpoint echoes JSON."""
    if not isinstance(authorization, RunAuthorization) or authorization.run_id != run_id:
        return RunResult("FAILED", reason="Invalid run authorization")
    if not isinstance(source, str):
        raise TypeError("JavaScript source must be a string")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be positive and finite")

    if len(source) > MESSAGE_BYTES:
        return RunResult("FAILED", reason="Source message too large")
    initial = _encode({"source": source, "messageBytes": MESSAGE_BYTES})
    if len(initial) > MESSAGE_BYTES:
        return RunResult("FAILED", reason="Source message too large")
    deadline = time.monotonic() + timeout

    node = shutil.which("node")
    if node is None:
        return RunResult("FAILED", reason="Node.js unavailable")
    try:
        driver = Path(__file__).with_name("runtime_child.cjs").read_text(encoding="utf-8")
        child = subprocess.Popen(
            [os.path.abspath(node), "--permission", "--no-allow-net", "--experimental-vm-modules",
             "--disallow-code-generation-from-strings", "--unhandled-rejections=strict",
             "-e", driver],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={}, cwd="/", bufsize=0,
        )
    except OSError:
        return RunResult("FAILED", reason="Could not launch JavaScript runtime")

    with child:
        try:
            return _exchange(child, initial, run_id, authorization, deadline)
        except TimeoutError:
            return RunResult("TIMED_OUT", reason="Run deadline exceeded")
        except _RunFailure as error:
            return RunResult("FAILED", reason=str(error))
        except (OSError, ValueError, RecursionError):
            return RunResult("FAILED", reason="Invalid runtime protocol")
        finally:
            child.kill()
            child.wait()


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _exchange(child, initial, run_id, authorization, deadline):
    outgoing = initial + b"\n"
    buffered = bytearray()
    totals = {"stdout": 0, "stderr": 0}
    limits = {"stdout": STDOUT_BYTES, "stderr": STDERR_BYTES}
    calls = 0
    terminal = None

    with selectors.DefaultSelector() as selector:
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(child, name)
            os.set_blocking(stream.fileno(), False)
            event = selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ
            selector.register(stream, event, name)

        while selector.get_map():
            for key, _ in selector.select(_remaining(deadline)):
                _remaining(deadline)
                channel = key.data
                try:
                    if channel == "stdin":
                        outgoing = outgoing[os.write(key.fd, outgoing):]
                        if not outgoing:
                            selector.unregister(key.fileobj)
                        continue
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                totals[channel] += len(chunk)
                if totals[channel] > limits[channel]:
                    raise _RunFailure(f"Runtime {channel} limit exceeded")
                if channel == "stderr":
                    continue

                buffered.extend(chunk)
                while (newline := buffered.find(b"\n")) != -1:
                    _remaining(deadline)
                    if newline > MESSAGE_BYTES or terminal is not None or outgoing:
                        raise _RunFailure("Invalid runtime message")
                    raw = bytes(buffered[:newline])
                    del buffered[:newline + 1]
                    message = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
                    if type(message) is not dict:
                        raise _RunFailure("Invalid runtime message")
                    kind = message.get("type")
                    if kind == "error" and message.keys() == {"type"}:
                        terminal = RunResult("FAILED", reason="JavaScript execution or serialization failed")
                    elif kind == "result" and message.keys() == {"type", "result"}:
                        value = message["result"]
                        if len(_encode(value)) > RESULT_BYTES:
                            raise _RunFailure("Final result too large")
                        terminal = RunResult("SUCCESS", value=value)
                    elif kind == "call" and message.keys() == {"type", "capability", "args"}:
                        capability = message["capability"]
                        calls += 1
                        if type(capability) is not str or calls > MAX_CALLS:
                            raise _RunFailure("Invalid capability call or call limit exceeded")
                        if authorization.allows(run_id, capability) and capability == ECHO_CAPABILITY:
                            reply = {"result": message["args"]}
                        else:
                            reply = {"error": "Denied"}
                        payload = _encode(reply)
                        if len(payload) > MESSAGE_BYTES:
                            raise _RunFailure("Capability response too large")
                        outgoing = payload + b"\n"
                        selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
                    else:
                        raise _RunFailure("Invalid runtime message")
                if len(buffered) > MESSAGE_BYTES:
                    raise _RunFailure("Runtime message too large")

    try:
        exit_code = child.wait(timeout=_remaining(deadline))
    except subprocess.TimeoutExpired as error:
        raise TimeoutError from error
    _remaining(deadline)
    if buffered or terminal is None or exit_code:
        raise _RunFailure("JavaScript runtime terminated without a valid outcome")
    return terminal

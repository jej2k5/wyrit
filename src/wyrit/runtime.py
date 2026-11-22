"""Disposable JavaScript execution with a run-scoped fake echo capability."""

from dataclasses import dataclass
from pathlib import Path
import math
import os
import selectors
import shutil
import subprocess
import time

from .capabilities import RunAuthorization


from .invocation import ECHO_CAPABILITY, MESSAGE_BYTES, InvocationProtocol, encode


RESULT_BYTES = 16 * 1024
STDOUT_BYTES = 256 * 1024
STDERR_BYTES = 8 * 1024


@dataclass(frozen=True)
class RunResult:
    status: str
    value: object = None
    reason: str | None = None


class _RunFailure(Exception):
    pass


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
    protocol = InvocationProtocol(run_id, authorization)
    initial = encode(protocol.envelope("start", source=source, messageBytes=MESSAGE_BYTES))
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
            return _exchange(child, initial, protocol, deadline)
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


def _exchange(child, initial, protocol, deadline):
    outgoing = initial + b"\n"
    buffered = bytearray()
    totals = {"stdout": 0, "stderr": 0}
    limits = {"stdout": STDOUT_BYTES, "stderr": STDERR_BYTES}
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
                    try:
                        message = protocol.decode(raw)
                        kind = message["type"]
                        if kind == "error":
                            terminal = RunResult("FAILED", reason="JavaScript execution or serialization failed")
                        elif kind == "result":
                            value = message["result"]
                            if len(encode(value)) > RESULT_BYTES:
                                raise _RunFailure("Final result too large")
                            terminal = RunResult("SUCCESS", value=value)
                        else:
                            reply = protocol.respond(message)
                            payload = encode(reply)
                            if len(payload) > MESSAGE_BYTES:
                                payload = encode(protocol.envelope(
                                    "call_error", call_id=message["call_id"], code="RESULT_TOO_LARGE",
                                ))
                            outgoing = payload + b"\n"
                            selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
                    except (ValueError, RecursionError) as error:
                        raise _RunFailure("Invalid runtime message") from error
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

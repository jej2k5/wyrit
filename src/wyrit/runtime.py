"""Disposable JavaScript execution with trusted host capability dispatch."""

from concurrent.futures import Future, wait
from dataclasses import dataclass, replace
from pathlib import Path
import math
import os
import selectors
import shutil
import subprocess
from threading import BoundedSemaphore, Thread
import time

from .capabilities import RunAuthorization
from .invocation import (
    ActionRecord, CallError, ECHO_CAPABILITY, MESSAGE_BYTES, InvocationProtocol, encode,
)
from .native_sandbox import SandboxUnavailable, native_command


RESULT_BYTES = 16 * 1024
STDOUT_BYTES = 256 * 1024
STDERR_BYTES = 8 * 1024
CANCELLATION_POLL_SECONDS = 0.02
_DISPATCH_SLOTS = BoundedSemaphore(8)


@dataclass(frozen=True)
class RunResult:
    status: str
    value: object = None
    reason: str | None = None
    unknown_calls: tuple[int, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    cancellation_requested: bool = False
    cancellation_confirmed: bool = False


class _RunFailure(Exception):
    pass


class _RunCancelled(Exception):
    pass


def run_javascript(source, *, run_id, authorization, timeout=2.0, dispatch=None,
                   cancel_event=None):
    """Run an async function body once, with optional cancellation via threading.Event."""
    if not isinstance(authorization, RunAuthorization) or authorization.run_id != run_id:
        return RunResult("FAILED", reason="Invalid run authorization")
    if not isinstance(source, str):
        raise TypeError("JavaScript source must be a string")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be positive and finite")

    if len(source) > MESSAGE_BYTES:
        return RunResult("FAILED", reason="Source message too large")
    deadline = time.monotonic() + timeout

    def remaining():
        if cancel_event is not None and cancel_event.is_set():
            raise _RunCancelled
        return _remaining(deadline)

    def wait_interval():
        seconds = remaining()
        return seconds if cancel_event is None else min(seconds, CANCELLATION_POLL_SECONDS)

    def invoke(capability, args, *, deadline):
        completion = Future()
        slots = _DISPATCH_SLOTS

        def dispatch_once():
            try:
                try:
                    remaining()
                except (TimeoutError, _RunCancelled):
                    raise CallError("DISPATCH_UNAVAILABLE", dispatched=False) from None
                result = dispatch(capability, args, deadline=deadline)
            except CallError as error:
                outcome = (None, error)
            except BaseException:
                outcome = (None, CallError("OUTCOME_UNKNOWN"))
            else:
                outcome = (result, None)
            finally:
                slots.release()
            value, error = outcome
            if error is None:
                completion.set_result(value)
            else:
                completion.set_exception(error)

        # The controller owns the deadline and ledger even if a host adapter stalls.
        if not slots.acquire(blocking=False):
            raise CallError("DISPATCH_UNAVAILABLE", dispatched=False)
        try:
            Thread(target=dispatch_once, daemon=True).start()
        except RuntimeError as error:
            slots.release()
            raise CallError("DISPATCH_UNAVAILABLE", dispatched=False) from error
        while not completion.done():
            wait((completion,), timeout=wait_interval())
        return completion.result()

    if cancel_event is not None and cancel_event.is_set():
        return RunResult("CANCELLED", reason="Run cancellation requested",
                         cancellation_requested=True, cancellation_confirmed=True)
    protocol = InvocationProtocol(
        run_id, authorization, dispatch=invoke if dispatch is not None else None, deadline=deadline,
    )
    initial = encode(protocol.envelope("start", source=source, messageBytes=MESSAGE_BYTES))
    if len(initial) > MESSAGE_BYTES:
        return RunResult("FAILED", reason="Source message too large")

    node = shutil.which("node")
    if node is None:
        return RunResult("FAILED", reason="Node.js unavailable")
    try:
        driver = Path(__file__).with_name("runtime_child.cjs").read_text(encoding="utf-8")
        with native_command(node, [
            "--permission", "--no-allow-net", "--experimental-vm-modules",
            "--disallow-code-generation-from-strings", "--unhandled-rejections=strict",
            "-e", driver,
        ], deadline=deadline) as (command, descriptors):
            remaining()
            child = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={}, cwd="/", bufsize=0, pass_fds=descriptors,
            )
    except SandboxUnavailable as error:
        return RunResult("FAILED", reason=str(error))
    except _RunCancelled:
        return RunResult("CANCELLED", reason="Run cancellation requested",
                         cancellation_requested=True, cancellation_confirmed=True)
    except TimeoutError:
        return RunResult("TIMED_OUT", reason="Run deadline exceeded")
    except OSError:
        return RunResult("FAILED", reason="Could not launch JavaScript runtime")

    with child:
        try:
            result = _exchange(child, initial, protocol, remaining, wait_interval)
        except _RunCancelled:
            result = RunResult("CANCELLED", reason="Run cancellation requested",
                               cancellation_requested=True)
        except TimeoutError:
            result = RunResult("TIMED_OUT", reason="Run deadline exceeded")
        except _RunFailure as error:
            result = RunResult("FAILED", reason=str(error))
        except (OSError, ValueError, RecursionError):
            result = RunResult("FAILED", reason="Invalid runtime protocol")
        finally:
            child.kill()
            child.wait()
    actions = protocol.finalize_actions()
    return replace(
        result, actions=actions,
        unknown_calls=tuple(action.call_id for action in actions if action.status == "UNKNOWN"),
        cancellation_confirmed=result.cancellation_requested,
    )


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _exchange(child, initial, protocol, remaining, wait_interval):
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
            for key, _ in selector.select(wait_interval()):
                remaining()
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
                    remaining()
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
                            remaining()
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

    while True:
        try:
            exit_code = child.wait(timeout=wait_interval())
            break
        except subprocess.TimeoutExpired:
            remaining()
    remaining()
    if buffered or terminal is None or exit_code:
        raise _RunFailure("JavaScript runtime terminated without a valid outcome")
    return terminal

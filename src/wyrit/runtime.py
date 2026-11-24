"""Disposable JavaScript execution with trusted host capability dispatch."""

from concurrent.futures import Future, wait
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
import math
import os
import selectors
import shutil
import subprocess
from threading import BoundedSemaphore, Lock, Thread
import time

from .budgets import Limit, LimitExceeded, RunBudgets
from .ledger import append_record, open_ledger
from .capabilities import RunAuthorization
from .invocation import (
    ActionRecord, CallError, DispatchInterrupted, ECHO_CAPABILITY, MESSAGE_BYTES, InvocationProtocol, encode,
)
from .native_sandbox import SandboxUnavailable, native_command


STDOUT_BYTES = 256 * 1024
STDERR_BYTES = 8 * 1024
CANCELLATION_POLL_SECONDS = 0.02
_MAX_DISPATCH_THREADS = 8
_DISPATCH_SLOTS = BoundedSemaphore(_MAX_DISPATCH_THREADS)


@dataclass(frozen=True)
class RunResult:
    status: str
    value: object = None
    reason: str | None = None
    unknown_calls: tuple[int, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    cancellation_requested: bool = False
    cancellation_confirmed: bool = False
    limits: dict = field(default_factory=dict, compare=False)
    limit_hit: dict | None = field(default=None, compare=False)
    ledger_error: str | None = field(default=None, compare=False)


class _RunFailure(Exception):
    pass


class _RunCancelled(Exception):
    pass


def run_javascript(source, *, run_id, authorization, timeout=None, dispatch=None,
                   cancel_event=None, budgets=None, ledger_path=None, tracks_send=False, _capture=None,
                   recipe=None, arguments=None, ledger_sink=None, context_id=None,
                   local_echo=True, isolate_dispatch=False):
    """Run an async function body once, with optional cancellation via threading.Event."""
    if not isinstance(source, str):
        raise TypeError("JavaScript source must be a string")
    budgets = budgets or RunBudgets()
    if timeout is not None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive and finite")
        budgets = replace(budgets, global_limits={**budgets.global_limits, "run_deadline": timeout})
    allowed = authorization.capability_ids if (
        isinstance(authorization, RunAuthorization) and authorization.run_id == run_id
    ) else ()
    namespaces = {capability.rpartition(".")[0] for capability in allowed}
    budgets = replace(budgets, server_limits={
        name: limits for name, limits in budgets.server_limits.items() if name in namespaces
    })
    report = budgets.report()
    started = time.time()
    audit = []
    actions = []
    call_timeouts = []
    with ExitStack() as stack:
        if ledger_sink is None:
            try:
                ledger = stack.enter_context(open_ledger(ledger_path))
            except (OSError, RuntimeError):
                return RunResult("FAILED", reason="Could not open run ledger", limits=report,
                                 ledger_error="Could not open run ledger")
        try:
            slots = BoundedSemaphore(_MAX_DISPATCH_THREADS) if isolate_dispatch else _DISPATCH_SLOTS
            result = _run(source, run_id, authorization, dispatch, cancel_event,
                          budgets, audit, actions, call_timeouts, tracks_send, recipe, arguments,
                          local_echo, slots)
        except Exception:
            result = RunResult("FAILED", reason="Unexpected runtime failure")
        limit_hits = [*call_timeouts]
        if result.limit_hit is not None:
            limit_hits.append(result.limit_hit)
        result = replace(
            result, actions=tuple(actions),
            unknown_calls=tuple(action.call_id for action in actions if action.status == "UNKNOWN"),
            limits=report, limit_hit=result.limit_hit or next(iter(call_timeouts), None),
        )
        ended = time.time()
        source_status = _capture.finish(audit) if _capture is not None else None
        reference = {"context_id": context_id} if context_id is not None else {}
        record = {
            "run_id": run_id,
            **reference,
            **(recipe or {}),
            "started_at": started,
            "ended_at": ended,
            "outcome": result.status,
            "reason": result.reason,
            **({"source_status": source_status} if source_status else {}),
            "limit_hit": result.limit_hit,
            "limit_hits": limit_hits,
        }
        try:
            if ledger_sink is None:
                append_record(ledger, {**record, "actions": [{**action, **reference} for action in audit]})
            else:
                ledger_sink(deepcopy({**record, "type": "run"}))
                for action in audit:
                    ledger_sink({**action, **reference, "type": "action", "run_id": run_id})
        except Exception:
            return replace(result, ledger_error="Could not append run ledger")
        return result


class _BudgetState:
    def __init__(self, budgets):
        self.budgets = budgets
        self.started = time.monotonic()
        self.global_limits = budgets.effective()
        self.active = {name: (self.global_limits[name], None)
                       for name in ("run_deadline", "max_calls")}
        self.servers = set()
        self.call_limits = self.global_limits
        self.namespace = None

    @property
    def deadline(self):
        return self.started + self.active["run_deadline"][0].value

    def exceeded(self, name, reason):
        limit, namespace = self.active[name]
        return LimitExceeded(name, limit, reason, namespace)

    def admit_call(self, count, capability):
        namespace = capability.rpartition(".")[0] if capability else None
        if namespace not in self.budgets.server_limits:
            namespace = None
        self.namespace = namespace
        self.call_limits = self.budgets.effective(namespace)
        if namespace is not None and namespace not in self.servers:
            for name, (current, _) in self.active.items():
                limit = self.call_limits[name]
                if not self.servers or limit.value < current.value:
                    self.active[name] = (limit, namespace)
            self.servers.add(namespace)
        if count > self.active["max_calls"][0].value:
            raise self.exceeded("max_calls", "Maximum calls per run exceeded")
        if time.monotonic() >= self.deadline:
            raise self.exceeded("run_deadline", "Run deadline exceeded")
        return self.call_limits, namespace


def _run(source, run_id, authorization, dispatch, cancel_event, budgets, audit, actions,
         call_timeouts, tracks_send, recipe, arguments, local_echo, slots):
    if not isinstance(authorization, RunAuthorization) or authorization.run_id != run_id:
        return RunResult("FAILED", reason="Invalid run authorization")
    if len(source) > MESSAGE_BYTES:
        return RunResult("FAILED", reason="Source message too large")
    budget = _BudgetState(budgets)

    def remaining():
        if cancel_event is not None and cancel_event.is_set():
            raise _RunCancelled
        return _remaining(budget.deadline)

    def wait_interval():
        seconds = remaining()
        poll = 3600 if cancel_event is None else CANCELLATION_POLL_SECONDS
        return min(seconds, poll)

    def invoke(capability, args, *, deadline=None):
        completion = Future()
        lock = Lock()
        sent = False
        closed = False
        limit = budget.call_limits["call_timeout"]
        run_deadline = budget.deadline
        deadline = min(run_deadline, time.monotonic() + limit.value)
        call_deadline_first = deadline < run_deadline

        def on_send():
            nonlocal sent
            with lock:
                if closed or time.monotonic() >= deadline or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise CallError("DISPATCH_UNAVAILABLE", dispatched=False)
                sent = True

        def dispatch_once():
            try:
                if tracks_send:
                    value = dispatch(capability, args, deadline=deadline, on_send=on_send)
                else:
                    on_send()
                    value = dispatch(capability, args, deadline=deadline)
            except CallError as error:
                outcome = (None, error)
            except BaseException:
                outcome = (None, CallError("OUTCOME_UNKNOWN", dispatched=sent))
            else:
                outcome = (value, None)
            finally:
                slots.release()
            completion.set_result((time.monotonic(), *outcome))

        def timed_out():
            call_timeouts.append(LimitExceeded(
                "call_timeout", limit, "Capability call timed out", budget.namespace,
            ).detail)
            return CallError("CALL_TIMEOUT", dispatched=sent)

        if not slots.acquire(blocking=False):
            raise CallError("DISPATCH_UNAVAILABLE", dispatched=False)
        try:
            Thread(target=dispatch_once, daemon=True).start()
        except RuntimeError as error:
            slots.release()
            raise CallError("DISPATCH_UNAVAILABLE", dispatched=False) from error
        try:
            while True:
                if completion.done():
                    finished, value, error = completion.result()
                    if finished <= deadline:
                        if error is None:
                            return value
                        if error.code != "CALL_TIMEOUT" or error.host_reported:
                            raise error
                        if call_deadline_first:
                            raise timed_out()
                        raise TimeoutError
                now = time.monotonic()
                if call_deadline_first and now >= deadline:
                    raise timed_out()
                interval = min(wait_interval(), max(0, deadline - now))
                wait((completion,), timeout=interval)
        except (TimeoutError, _RunCancelled) as exc:
            with lock:
                closed = True
                if not sent:
                    raise DispatchInterrupted(exc) from None
            raise
        finally:
            with lock:
                closed = True

    if cancel_event is not None and cancel_event.is_set():
        return RunResult("CANCELLED", reason="Run cancellation requested",
                         cancellation_requested=True, cancellation_confirmed=True)
    protocol = InvocationProtocol(
        run_id, authorization, dispatch=invoke if dispatch is not None else None,
        admit_call=budget.admit_call, audit=audit,
        recipe=recipe is not None, local_echo=local_echo,
    )
    initial = encode(protocol.envelope(
        "start", source=source, messageBytes=MESSAGE_BYTES,
        **({"arguments": arguments} if recipe is not None else {}),
    ))
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
        ], deadline=budget.deadline) as (command, descriptors):
            remaining()
            bubblewrap = command[:1] == ["/usr/bin/prlimit"]
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
        return _limit_result(budget.exceeded("run_deadline", "Run deadline exceeded"))
    except OSError:
        return RunResult("FAILED", reason="Could not launch JavaScript runtime")

    with child:
        try:
            result = _exchange(child, initial, protocol, remaining, wait_interval,
                               budget.global_limits["max_output_bytes"], bubblewrap=bubblewrap)
        except _RunCancelled:
            result = RunResult("CANCELLED", reason="Run cancellation requested",
                               cancellation_requested=True)
        except TimeoutError:
            result = _limit_result(budget.exceeded("run_deadline", "Run deadline exceeded"))
        except LimitExceeded as error:
            result = _limit_result(error)
        except _RunFailure as error:
            result = RunResult("FAILED", reason=str(error))
        except (OSError, ValueError, RecursionError):
            result = RunResult("FAILED", reason="Invalid runtime protocol")
        finally:
            actions.extend(protocol.finalize_actions())
            child.kill()
            child.wait()
    return replace(result, cancellation_confirmed=result.cancellation_requested)


def _limit_result(error):
    status = "TIMED_OUT" if error.detail["name"] == "run_deadline" else "FAILED"
    return RunResult(status, reason=str(error), limit_hit=error.detail)


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _exchange(child, initial, protocol, remaining, wait_interval, output_limit, *, bubblewrap):
    outgoing = initial + b"\n"
    buffered = bytearray()
    stderr = bytearray()
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
                except BrokenPipeError:
                    if channel != "stdin":
                        raise
                    # The launcher can refuse isolation before reading any input.
                    selector.unregister(key.fileobj)
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                totals[channel] += len(chunk)
                if totals[channel] > limits[channel]:
                    raise LimitExceeded(f"runtime_{channel}_bytes", Limit(limits[channel], "default"),
                                        f"Runtime {channel} limit exceeded")
                if channel == "stderr":
                    if bubblewrap:
                        stderr.extend(chunk)
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
                            if message["code"] == "OUTPUT_TOO_LARGE":
                                if output_limit.value <= MESSAGE_BYTES // 2:
                                    raise LimitExceeded("max_output_bytes", output_limit, "Final result too large")
                                raise LimitExceeded("protocol_message_bytes", Limit(MESSAGE_BYTES, "default"),
                                                    "Final result exceeded the protocol message size")
                            terminal = RunResult("FAILED", reason="JavaScript execution or serialization failed")
                        elif kind == "result":
                            value = message["result"]
                            if len(encode(value)) > output_limit.value:
                                raise LimitExceeded("max_output_bytes", output_limit, "Final result too large")
                            terminal = RunResult("SUCCESS", value=value)
                        else:
                            remaining()
                            reply = protocol.respond(message)
                            payload = encode(reply)
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
        if bubblewrap and exit_code and not totals["stdout"]:
            if stderr.startswith(b"bwrap:"):
                if b"namespace" in stderr.lower():
                    raise _RunFailure("Bubblewrap could not establish required Linux namespaces")
                raise _RunFailure("Bubblewrap could not establish the required Linux sandbox")
            if stderr.startswith(b"prlimit:"):
                raise _RunFailure("Could not apply the required Linux runtime data limit")
        raise _RunFailure("JavaScript runtime terminated without a valid outcome")
    return terminal

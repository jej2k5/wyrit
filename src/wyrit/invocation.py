"""Versioned JSON-line messages on a single run's private pipes."""

from dataclasses import dataclass, replace
import json
import math
import time

from .budgets import Limit, LimitExceeded
from .capabilities import _ID


VERSION = 1
MESSAGE_BYTES = 64 * 1024
MAX_CALLS = 32
ECHO_CAPABILITY = "fake.echo"


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return value


def _check_value(value, depth=0):
    if depth > 64:
        raise ValueError("JSON nesting limit exceeded")
    if type(value) is dict:
        for child in value.values():
            _check_value(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _check_value(child, depth + 1)
    elif type(value) in (int, float) and (
        abs(value) > 2**53 - 1 or not math.isfinite(value)
    ):
        raise ValueError("Unsupported JSON number")


class CallError(Exception):
    """A host adapter failure with a public, fixed protocol code."""

    def __init__(self, code, *, dispatched=True, action_status=None, result_size=None, audit_reason=None):
        super().__init__(code)
        self.code = code
        self.dispatched = dispatched
        self.action_status = action_status
        self.result_size = result_size
        self.audit_reason = audit_reason
        self.audit_server = None


class DispatchInterrupted(Exception):
    """The run ended before the host sent this call."""

    def __init__(self, cause):
        self.cause = cause


@dataclass(frozen=True)
class ActionRecord:
    call_id: int
    capability: str
    status: str
    code: str | None = None


class InvocationProtocol:
    def __init__(self, run_id, authorization, *, dispatch=None, deadline=None, admit_call=None, audit=None):
        self.run_id = run_id
        self.authorization = authorization
        self.dispatch = dispatch
        self.deadline = deadline
        self.admit_call = admit_call
        self.audit = audit
        self.next_call_id = 1
        self._actions = {}

    def envelope(self, kind, **fields):
        return {"version": VERSION, "run_id": self.run_id, "type": kind, **fields}

    def decode(self, raw):
        if len(raw) > MESSAGE_BYTES:
            raise ValueError("Oversized message")
        message = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (type(message) is not dict or type(message.get("version")) is not int
                or message["version"] != VERSION or message.get("run_id") != self.run_id
                or type(message.get("type")) is not str):
            raise ValueError("Invalid envelope")
        fields = {
            "call": {"call_id", "capability", "args"},
            "result": {"result"},
            "error": {"code"},
        }.get(message["type"])
        if fields is None or message.keys() != {"version", "run_id", "type"} | fields:
            raise ValueError("Invalid message fields")
        if message["type"] == "error" and message["code"] not in ("EXECUTION_FAILED", "OUTPUT_TOO_LARGE"):
            raise ValueError("Invalid execution error")
        _check_value(message)
        return message

    def respond(self, message):
        call_id = message["call_id"]
        capability = message["capability"]
        if (type(call_id) is not int or call_id != self.next_call_id
                or type(capability) is not str or not _ID.fullmatch(capability)):
            raise ValueError("Invalid capability or call ID")
        allowed = message["run_id"] == self.run_id and self.authorization.allows(
            self.run_id, capability,
        )
        start = time.monotonic()
        outcome = "DENIED"
        size = None
        namespace = None
        reason = None
        server = None
        try:
            if self.admit_call is not None:
                limits, namespace = self.admit_call(call_id, capability if allowed else None)
            elif call_id > MAX_CALLS:
                raise ValueError("Invalid call ID")
            self.next_call_id += 1
            if not allowed or (capability != ECHO_CAPABILITY and self.dispatch is None):
                return self.envelope("call_error", call_id=call_id, code="DENIED")

            if capability == ECHO_CAPABILITY:
                result = message["args"]
            else:
                action = ActionRecord(call_id, capability, "DISPATCHED")
                self._actions[call_id] = action
                outcome = "UNKNOWN"
                try:
                    result = self.dispatch(capability, message["args"], deadline=self.deadline)
                except DispatchInterrupted as error:
                    outcome = "FAILED"
                    del self._actions[call_id]
                    raise error.cause from None
                except CallError as error:
                    size = error.result_size
                    reason = error.audit_reason
                    server = error.audit_server
                    if error.dispatched:
                        outcome = error.action_status or (
                            "UNKNOWN" if error.code in ("OUTCOME_UNKNOWN", "CALL_TIMEOUT") else "FAILED"
                        )
                        self._actions[call_id] = replace(action, status=outcome, code=error.code)
                    else:
                        outcome = "DENIED" if error.code == "DENIED" else "FAILED"
                        del self._actions[call_id]
                    if error.code == "RESULT_TOO_LARGE" and size is not None and self.admit_call is not None:
                        self._check_output(size, limits["max_output_bytes"], namespace)
                        if size > MESSAGE_BYTES:
                            raise self._wire_output_limit() from None
                    return self.envelope("call_error", call_id=call_id, code=error.code)
                self._actions[call_id] = replace(action, status="SUCCEEDED")
            outcome = "SUCCEEDED"
            if self.admit_call is not None:
                reply = self.envelope("call_result", call_id=call_id, result=result)
                size = len(encode(result))
                self._check_output(size, limits["max_output_bytes"], namespace)
                if len(encode(reply)) > MESSAGE_BYTES:
                    raise self._wire_output_limit()
                return reply
            return self.envelope("call_result", call_id=call_id, result=result)
        finally:
            if self.audit is not None:
                record = {
                    "call_id": call_id,
                    "capability": capability if allowed else None,
                    "outcome": outcome,
                    "duration_seconds": time.monotonic() - start,
                    "result_size_bytes": size,
                }
                if reason is not None:
                    record["reason"] = reason
                if server is not None:
                    record["server"] = server
                self.audit.append(record)

    @staticmethod
    def _check_output(size, limit, namespace):
        if size > limit.value:
            raise LimitExceeded("max_output_bytes", limit, "Capability result too large", namespace)

    @staticmethod
    def _wire_output_limit():
        return LimitExceeded("protocol_message_bytes", Limit(MESSAGE_BYTES, "default"),
                             "Capability result exceeded the protocol message size")

    def finalize_actions(self):
        for call_id, action in self._actions.items():
            if action.status == "DISPATCHED":
                self._actions[call_id] = replace(action, status="UNKNOWN", code="OUTCOME_UNKNOWN")
        return tuple(self._actions.values())

"""Versioned JSON-line messages on a single run's private pipes."""

import json
import math

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


class InvocationProtocol:
    def __init__(self, run_id, authorization):
        self.run_id = run_id
        self.authorization = authorization
        self.next_call_id = 1

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
        if message["type"] == "error" and message["code"] != "EXECUTION_FAILED":
            raise ValueError("Invalid execution error")
        _check_value(message)
        return message

    def respond(self, message):
        call_id = message["call_id"]
        capability = message["capability"]
        if (type(call_id) is not int or call_id != self.next_call_id or call_id > MAX_CALLS
                or type(capability) is not str or not _ID.fullmatch(capability)):
            raise ValueError("Invalid capability or call ID")
        self.next_call_id += 1
        if capability != ECHO_CAPABILITY or not self.authorization.allows(self.run_id, capability):
            return self.envelope("call_error", call_id=call_id, code="DENIED")
        return self.envelope("call_result", call_id=call_id, result=message["args"])

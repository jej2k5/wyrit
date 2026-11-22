"""Expose tools from a host-owned MCP session as capabilities."""

import base64
import binascii
import re
import time

from anyio import fail_after
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import types
from referencing import Registry
from referencing.exceptions import Unresolvable

from .capabilities import Capability, _check_id, _check_json
from .invocation import ECHO_CAPABILITY, MESSAGE_BYTES, CallError, _check_value, encode


BINARY_BYTES = 8 * 1024
MAX_DISCOVERY_PAGES = 32
_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_NATIVE_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_ENCODED_PREFIX = "encoded-"


def _validator(schema):
    if not isinstance(schema, (dict, bool)):
        raise ValueError("MCP schemas must be JSON objects or booleans")
    if isinstance(schema, dict) and schema.get("$schema", _SCHEMA_DIALECT) != _SCHEMA_DIALECT:
        raise ValueError("Unsupported MCP schema dialect")
    Draft202012Validator.check_schema(schema)
    # An empty registry has no network or filesystem retrieval callback.
    return Draft202012Validator(schema, registry=Registry())


def _capability_id(namespace, name):
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ValueError("Invalid MCP tool name")
    if not _NATIVE_NAME.fullmatch(name) or name.startswith(_ENCODED_PREFIX):
        name = _ENCODED_PREFIX + name.encode("ascii").hex()
    return f"{namespace}.{name}"


class MCPAdapter:
    """Discover once and dispatch through an initialized SDK session on its portal.

    The caller owns the transport, authentication, portal and session lifetime.
    Pass dispatch to run_javascript and use capabilities to build the host registry.
    """

    def __init__(self, namespace, session, portal, *, discovery_timeout=5.0):
        _check_id(namespace)
        self._session = session
        self._portal = portal
        self._tools = {}
        deadline = time.monotonic() + discovery_timeout
        cursor = None
        seen = set()
        for _ in range(MAX_DISCOVERY_PAGES):
            params = {} if cursor is None else {"cursor": cursor}
            page = self._request("tools/list", params, deadline)
            tools = page.get("tools")
            if not isinstance(tools, list):
                raise ValueError("Invalid MCP discovery page")
            for tool in tools:
                self._add_tool(namespace, tool)
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Invalid MCP discovery cursor")
            seen.add(cursor)
        else:
            raise ValueError("MCP discovery page limit exceeded")

    @property
    def capabilities(self):
        return tuple(tool[0] for tool in self._tools.values())

    def _add_tool(self, namespace, tool):
        name = tool["name"]
        capability_id = _capability_id(namespace, name)
        if capability_id in self._tools or capability_id == ECHO_CAPABILITY:
            raise ValueError(f"Duplicate or reserved capability ID: {capability_id}")
        input_schema = tool["inputSchema"]
        output_schema = tool.get("outputSchema")
        inputs = _validator(input_schema)
        outputs = None if output_schema is None else _validator(output_schema)
        capability = Capability(
            capability_id,
            tool.get("description") or tool.get("title") or f"MCP tool {name}",
            input_schema,
            output_schema if outputs is not None else True,
            {
                "adapter": "mcp",
                "tool_name": name,
                **{key: tool[key] for key in ("title", "annotations") if key in tool},
            },
        )
        self._tools[capability_id] = (capability, name, inputs, outputs)

    def _request(self, method, params, deadline):
        # Use the SDK session for framing and correlation. Read the raw result
        # so schema resolution and content policy stay in this adapter.
        async def send():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with fail_after(remaining):
                result = await self._session.send_request(
                    types.Request[dict, str](method=method, params=params), types.Result,
                )
            return result.model_extra

        if time.monotonic() >= deadline:
            raise TimeoutError
        future = self._portal.start_task_soon(send)
        return future.result(timeout=max(0, deadline - time.monotonic()))

    def dispatch(self, capability_id, args, *, deadline):
        """Invoke a capability after the run boundary has checked its grant."""
        try:
            _, name, inputs, outputs = self._tools[capability_id]
        except KeyError:
            raise CallError("DENIED") from None
        try:
            if not isinstance(args, dict):
                raise ValueError
            _check_json(args)
            _check_value(args, 1)
            inputs.validate(args)
        except (ValidationError, Unresolvable, TypeError, ValueError, RecursionError):
            raise CallError("INVALID_ARGUMENTS") from None

        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            response = self._request("tools/call", {"name": name, "arguments": args}, deadline)
        except Exception:
            # The server may have acted even if the session reports an error.
            raise CallError("OUTCOME_UNKNOWN") from None

        try:
            result = _adapt(response, outputs)
            _check_json(result)
            _check_value(result, 1)
            if len(encode(result)) > MESSAGE_BYTES:
                raise CallError("RESULT_TOO_LARGE")
            return result
        except (ValidationError, Unresolvable, TypeError, ValueError, RecursionError):
            raise CallError("INVALID_RESULT") from None


def _adapt(response, outputs):
    is_error = response.get("isError", False)
    if type(is_error) is not bool or not isinstance(response.get("content"), list):
        raise ValueError("Invalid MCP result")
    if is_error:
        raise CallError("TOOL_ERROR")
    if "structuredContent" in response:
        value = response["structuredContent"]
        if outputs is not None:
            outputs.validate(value)
        return value
    if outputs is not None:
        raise ValueError("Missing structured output")

    content = []
    remaining = BINARY_BYTES
    for block in response["content"]:
        if not isinstance(block, dict):
            raise ValueError("Invalid MCP content block")
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            content.append({"type": kind, "text": block["text"]})
        elif kind in ("image", "audio"):
            data, mime_type = block.get("data"), block.get("mimeType")
            if not isinstance(data, str) or not isinstance(mime_type, str):
                raise ValueError("Invalid MCP binary block")
            if len(data) > 4 * ((remaining + 2) // 3):
                raise CallError("RESULT_TOO_LARGE")
            try:
                remaining -= len(base64.b64decode(data, validate=True))
            except (ValueError, binascii.Error):
                raise ValueError("Invalid MCP binary encoding") from None
            if remaining < 0:
                raise CallError("RESULT_TOO_LARGE")
            content.append({"type": kind, "data": data, "mimeType": mime_type})
        else:
            raise ValueError("Unsupported MCP content block")
    return {"content": content}

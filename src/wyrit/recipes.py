"""File-backed reviewed scripts."""

from datetime import datetime, timezone
from difflib import unified_diff
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from jsonschema.exceptions import SchemaError

from .capabilities import _ID
from .capture import contains_credential
from .host_config import ConfigError
from .invocation import MESSAGE_BYTES, _check_value, _unique_object, encode
from .mcp_adapter import _contains_secret, _validator

_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FILE_BYTES = 256 * 1024


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def source_digest(recipe):
    return digest([recipe[key] for key in ("source", "input_schema", "capabilities")])


def capability_record(capability):
    return {"schema_digest": digest(capability.input_schema), "class": capability.classification}


def _content(data, name, secrets):
    if (not isinstance(data, dict) or data.get("name") != name
            or not isinstance(data.get("description"), str) or not data["description"].strip()
            or not isinstance(data.get("source"), str) or not data["source"].strip()
            or len(encode(data["source"])) >= MESSAGE_BYTES
            or not isinstance(data.get("input_schema"), dict)
            or not isinstance(data.get("capabilities"), dict)):
        raise ValueError("Invalid recipe definition")
    data["description"].encode("utf-8")
    data["source"].encode("utf-8")
    _check_value(data)
    if contains_credential(data) or _contains_secret(data, secrets):
        raise ValueError("Recipe contains sensitive data")
    _validator(data["input_schema"])
    for key, record in data["capabilities"].items():
        if (not _ID.fullmatch(key) or not isinstance(record, dict)
                or record.keys() != {"class", "schema_digest"}
                or record["class"] not in ("read", "write")
                or not isinstance(record["schema_digest"], str)
                or not _DIGEST.fullmatch(record["schema_digest"])):
            raise ValueError("Invalid recipe capability record")


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular recipe file")
        raw = stream.read(_FILE_BYTES + 1)
    if len(raw) > _FILE_BYTES:
        raise ValueError("Recipe file too large")
    return raw, json.loads(raw, object_pairs_hook=_unique_object)


def _approved(data):
    approval = data.get("approval")
    if not isinstance(approval, dict):
        return False
    operator = approval.get("approved_by")
    timestamp = approval.get("approved_at")
    if not isinstance(operator, str) or not operator.strip() or not isinstance(timestamp, str):
        return False
    try:
        checked = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return checked.utcoffset() is not None and approval.get("source_digest") == source_digest(data)


def _state(data, capabilities):
    if "approval" not in data:
        return "pending", None
    if not _approved(data):
        return "unapproved", "Recipe content or approval changed."
    for key, record in sorted(data["capabilities"].items()):
        if key not in capabilities or capability_record(capabilities[key]) != record:
            return "stale", f"Capability {key} changed or is unavailable."
    return "runnable", None


def scan(source):
    node = shutil.which("node")
    if node is None:
        raise ConfigError("Node.js is required to validate recipe source.")
    try:
        checked = subprocess.run(
            [node, "--permission", "--expose-internals", "-e",
             Path(__file__).with_name("recipe_scan.cjs").read_text(encoding="utf-8")],
            input=source, text=True, capture_output=True, timeout=3, env={}, cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ConfigError("Could not validate recipe source.") from None
    if checked.returncode != 0:
        raise ConfigError('Use valid JavaScript with direct tools.call("literal-id", arguments) calls.')
    return json.loads(checked.stdout)


def _write(path, raw, *, new=False):
    if len(raw) > _FILE_BYTES:
        raise ConfigError("Recipe file is too large.")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".wyrit-", delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            if new:
                os.link(staged, path)
            else:
                os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def _review_revision(raw, data, capabilities):
    return digest([sha256(raw).hexdigest(), {
        key: capability_record(capabilities[key]) if key in capabilities else None
        for key in data["capabilities"]
    }])


def _pretty(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _review_lines(raw):
    data = json.loads(raw)
    source = data.pop("source")
    data.pop("approval", None)
    return [*_pretty(data).decode().splitlines(), "", "JavaScript", *source.split("\n")]


class Recipes:
    """Access the configured directory under the host's admission lock."""

    def __init__(self, directory=None):
        self.directory = directory
        self.reviewed = {}

    def path(self, name):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigError("Use a recipe name matching [a-z][a-z0-9-]{0,63}.")
        if self.directory is None:
            raise ConfigError("Configure recipes_path to use recipes.")
        return self.directory / (name + ".json")

    def get(self, name, capabilities, secrets):
        return self._entry(self.path(name), capabilities, secrets)

    def entries(self, capabilities, secrets, *, details=False):
        if self.directory is None:
            return []
        return [
            entry for path in sorted(self.directory.glob("*.json")) if _NAME.fullmatch(path.stem)
            if (entry := self._entry(path, capabilities, secrets, details=details)) is not None
        ]

    def _entry(self, path, capabilities, secrets, *, details=False):
        name = path.stem
        try:
            raw, data = _read(path)
            _content(data, name, secrets)
            state, reason = _state(data, capabilities)
            if _approved(data):
                self.reviewed[name] = raw
            entry = {**data, "state": state, "reason": reason,
                     "revision": _review_revision(raw, data, capabilities)}
            if details:
                entry["current_capabilities"] = {
                    key: capability_record(capabilities[key]) if key in capabilities else None
                    for key in data["capabilities"]
                }
            if details and state == "unapproved":
                previous = self.reviewed.get(name)
                if previous is None:
                    try:
                        prior_raw, prior_data = _read(path.with_suffix(".json.approved"))
                        _content(prior_data, name, secrets)
                        if _approved(prior_data):
                            previous = prior_raw
                    except (OSError, ValueError, SchemaError, RecursionError):
                        pass
                if previous is not None:
                    diff = "\n".join(unified_diff(_review_lines(previous), _review_lines(raw),
                                                  "approved", "current", lineterm=""))
                    if not contains_credential(diff) and not _contains_secret(diff, secrets):
                        entry["diff"] = diff
            return entry
        except FileNotFoundError:
            return None
        except (OSError, ValueError, SchemaError, RecursionError):
            return {"name": name, "state": "unapproved",
                    "reason": "Recipe file is invalid or contains sensitive data."}

    def propose(self, name, description, input_schema, source, capabilities, secrets):
        path = self.path(name)
        if path.exists() or path.is_symlink():
            raise ConfigError("A recipe file with that name already exists.")
        data = {"name": name, "description": description, "input_schema": input_schema,
                "source": source, "capabilities": {}}
        try:
            _content(data, name, secrets)
        except (ValueError, SchemaError, RecursionError):
            raise ConfigError("Invalid recipe source, description or input schema.") from None
        for key in scan(source):
            if key not in capabilities:
                raise ConfigError("A declared call targets an unavailable capability.")
            data["capabilities"][key] = capability_record(capabilities[key])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            _write(path, _pretty(data), new=True)
        except FileExistsError:
            raise ConfigError("A recipe file with that name already exists.") from None
        return {"name": name, "state": "pending"}

    def change(self, action, name, revision, operator, capabilities, secrets):
        path = self.path(name)
        raw, data = _read(path)
        _content(data, name, secrets)
        if _review_revision(raw, data, capabilities) != revision:
            raise ConfigError("The recipe changed. Reload the page and review it again.")
        if action == "delete":
            path.unlink()
            self.reviewed.pop(name, None)
            path.with_suffix(".json.approved").unlink(missing_ok=True)
            return
        if action == "approve":
            if (not isinstance(operator, str) or not operator.strip()
                    or contains_credential(operator) or _contains_secret(operator, secrets)):
                raise ConfigError("Enter an operator label without sensitive data.")
            if set(scan(data["source"])) != data["capabilities"].keys():
                raise ConfigError("Review the file and declare exactly the capabilities used by the source.")
            for key in data["capabilities"]:
                if key not in capabilities:
                    raise ConfigError(f"Capability {key} is unavailable.")
            data["capabilities"] = {key: capability_record(capabilities[key])
                                    for key in data["capabilities"]}
            data["approval"] = {"approved_at": datetime.now(timezone.utc).isoformat(),
                                "approved_by": operator.strip(), "source_digest": source_digest(data)}
        else:
            data.pop("approval", None)
        current, _ = _read(path)
        if current != raw:
            raise ConfigError("The recipe changed. Reload the page and review it again.")
        updated = _pretty(data)
        _write(path.with_suffix(".json.bak"), raw)
        _write(path, updated)
        if action == "approve":
            self.reviewed[name] = updated
            try:
                _write(path.with_suffix(".json.approved"), updated)
            except OSError:
                # The approved file is already committed. Keep the diff baseline in memory.
                pass

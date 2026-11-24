"""Capability discovery and run grants for the trusted host."""

from dataclasses import dataclass
import json
import re


_ID = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*")
_TOKEN = re.compile(r"[^\W_]+")


def _check_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid capability ID: {value!r}")


def _check_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON keys must be strings")
            _check_json(child)
    elif isinstance(value, list):
        for child in value:
            _check_json(child)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise TypeError("Expected a JSON value")


def _serialize(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, init=False)
class Capability:
    """An explicitly named capability with immutable descriptor data."""

    id: str
    description: str
    classification: str
    _descriptor: str

    def __init__(self, id: str, description: str, input_schema: dict | bool,
                 output_schema: dict | bool = True, adapter_metadata: dict | None = None,
                 *, classification: str = "write") -> None:
        _check_id(id)
        if not isinstance(description, str) or not description.strip():
            raise ValueError("A capability requires a description")
        for schema in (input_schema, output_schema):
            if not isinstance(schema, (dict, bool)):
                raise TypeError("A schema must be a JSON object or boolean")
        if adapter_metadata is not None and not isinstance(adapter_metadata, dict):
            raise TypeError("Adapter metadata must be a JSON object")
        if classification not in ("read", "write"):
            raise ValueError("Expected a read or write classification")
        descriptor = {
            "id": id,
            "description": description,
            "classification": classification,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "adapter_metadata": adapter_metadata if adapter_metadata is not None else {},
        }
        _check_json(descriptor)
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "classification", classification)
        object.__setattr__(self, "_descriptor", _serialize(descriptor))

    @property
    def input_schema(self) -> dict | bool:
        return self.to_dict()["input_schema"]

    @property
    def output_schema(self) -> dict | bool:
        return self.to_dict()["output_schema"]

    @property
    def adapter_metadata(self) -> dict:
        return self.to_dict()["adapter_metadata"]

    def to_dict(self) -> dict:
        return json.loads(self._descriptor)

    def to_json(self) -> str:
        return self._descriptor


@dataclass(frozen=True)
class RunAuthorization:
    run_id: str
    capability_ids: frozenset[str]

    def __post_init__(self):
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("Authorization requires a run ID")
        if isinstance(self.capability_ids, str):
            raise TypeError("Expected a collection of capability IDs")
        ids = frozenset(self.capability_ids)
        for capability_id in ids:
            _check_id(capability_id)
        object.__setattr__(self, "capability_ids", ids)

    def allows(self, run_id, capability_id):
        return run_id == self.run_id and capability_id in self.capability_ids

    def to_dict(self):
        return {"run_id": self.run_id, "capability_ids": sorted(self.capability_ids)}


class CapabilityRegistry:
    def __init__(self, capabilities=()):
        self._capabilities = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability):
        if capability.id in self._capabilities:
            raise ValueError(f"Duplicate capability ID: {capability.id}")
        self._capabilities[capability.id] = capability

    @property
    def ids(self):
        return tuple(self._capabilities)

    def resolve(self, capability_id):
        """Return the host descriptor for an exact ID, or raise KeyError."""
        return self._capabilities[capability_id]

    def describe(self, capability_id):
        descriptor = self.resolve(capability_id).to_dict()
        del descriptor["adapter_metadata"]
        return descriptor

    def search(self, query, *, limit=10, offset=0):
        """Return a page of ranked capability summaries."""
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise ValueError("Search requires a positive limit and nonnegative offset")
        query = query.strip().casefold()
        terms = set(_TOKEN.findall(query))
        if not terms:
            return []

        matches = []
        for capability in self._capabilities.values():
            id_terms = terms.intersection(_TOKEN.findall(capability.id))
            description_terms = terms.intersection(
                _TOKEN.findall(capability.description.casefold())
            )
            if id_terms or description_terms:
                matches.append((
                    query != capability.id,
                    -len(id_terms | description_terms),
                    -len(id_terms),
                    capability.id,
                ))

        matches.sort()
        summaries = []
        for _, _, _, capability_id in matches[offset:offset + limit]:
            capability = self.resolve(capability_id)
            summaries.append({"id": capability.id, "description": capability.description,
                              "classification": capability.classification})
        return summaries

    def authorization_for(self, run_id, capability_ids):
        authorization = RunAuthorization(run_id, capability_ids)
        for capability_id in sorted(authorization.capability_ids):
            self.resolve(capability_id)
        return authorization

    def to_json(self):
        return _serialize([
            self.resolve(capability_id).to_dict()
            for capability_id in sorted(self._capabilities)
        ])

"""Bounded tool descriptions for the agent-facing catalog."""

from collections import Counter
import json

_DESCRIPTION_BYTES = 8 * 1024
_CATALOG_LIMIT = 60
_EXECUTE_DESCRIPTION = (
    "Run a JavaScript async function body. Use return to produce a JSON value.\n"
    'Inside the script, await tools.call("capability.id", args) for a JSON result. '
    "A rejected call throws an Error with read-only code and capability fields.\n"
    "Codes: DENIED, TOOL_ERROR, OUTCOME_UNKNOWN, CALL_TIMEOUT, INVALID_ARGUMENTS, "
    "RESULT_TOO_LARGE, DISPATCH_UNAVAILABLE, INVALID_RESULT.\n"
    "DENIED also has reason WRITE_POLICY or NOT_AUTHORIZED."
)
_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def _schema_type(schema):
    if schema is False:
        return "never"
    kind = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(kind, str) and kind in _JSON_TYPES:
        return kind
    if isinstance(kind, list):
        return "|".join(sorted({item for item in kind
                                if isinstance(item, str) and item in _JSON_TYPES})) or "any"
    return "any"


def _signature(schema):
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if isinstance(properties, dict) and isinstance(required, list):
        required = {name for name in required if isinstance(name, str)}
        names = properties.keys() | required
        if names:
            return "{" + ", ".join(
                f"{json.dumps(name, ensure_ascii=False)}{'*' if name in required else ''}: "
                f"{_schema_type(properties.get(name))}" for name in sorted(names)
            ) + "}"
    return "{}" if _schema_type(schema) == "object" else _schema_type(schema)


def _execute_description(capabilities):
    capabilities = sorted(capabilities, key=lambda cap: cap.id)
    if not capabilities:
        return _EXECUTE_DESCRIPTION + "\n\nNo capabilities are currently exposed."
    if len(capabilities) <= _CATALOG_LIMIT:
        description = _EXECUTE_DESCRIPTION + "\n\nCapabilities (* required):"
        for cap in capabilities:
            description += (
                f"\n{cap.id} [{cap.classification}] ({_signature(cap.input_schema)}) "
                f"— {' '.join(cap.description.split())}"
            )
            encoded = description.encode("utf-8", errors="replace")
            if len(encoded) > _DESCRIPTION_BYTES:
                break
        else:
            return encoded.decode("utf-8")

    counts = Counter(cap.id.rpartition(".")[0] or "(unqualified)" for cap in capabilities)
    description = _EXECUTE_DESCRIPTION + "\n\nUse search to discover capabilities. Namespace counts:"
    overflow = "\nAdditional namespaces available through search."
    omitted = False
    for namespace, count in sorted(counts.items()):
        line = f"\n{namespace}: {count}"
        if len((description + line + overflow).encode("utf-8")) > _DESCRIPTION_BYTES:
            omitted = True
        else:
            description += line
    return description + overflow if omitted else description


def _brief(text, limit=256):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _recipe_description(description, recipes):
    if not recipes:
        return description
    heading = "\n\nRecipes (* required):"
    overflow = "\nUse search to discover more recipes."
    lines = [
        f"\n{recipe['name']} [{_recipe_class(recipe)}] ({_brief(_signature(recipe['input_schema']))}) "
        f"— {_brief(' '.join(recipe['description'].split()))}"
        for recipe in recipes
    ]
    # Reserve space for recipe discovery even when the capability catalog is full.
    available = min(_DESCRIPTION_BYTES * 3 // 4, _DESCRIPTION_BYTES - len((heading + overflow).encode()))
    if len(description.encode()) > available:
        description = description.encode()[:available].decode(errors="ignore").rsplit("\n", 1)[0]
    description += heading
    for line in lines:
        if len((description + line + overflow).encode()) > _DESCRIPTION_BYTES:
            return description + overflow
        description += line
    return description


def _recipe_class(recipe):
    return "write" if any(record["class"] == "write"
                          for record in recipe["capabilities"].values()) else "read"

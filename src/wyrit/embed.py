"""Trusted, run-scoped embedding API for Wyrit."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from types import MappingProxyType
from typing import Any, TypeAlias
from uuid import uuid4

from anyio import move_on_after, to_thread
from anyio.from_thread import BlockingPortal
from jsonschema.exceptions import ValidationError

from .budgets import RunBudgets
from .capabilities import Capability, CapabilityRegistry, RunAuthorization, _check_json
from .capture import RunCapture
from .catalog import _execute_description, _recipe_description
from .host_config import ConfigError
from .invocation import CALL_CODES, CallError, _check_value
from .mcp_adapter import _validator
from .recipes import Recipes
from .runtime import run_javascript

__all__ = ["Capability", "CapabilityError", "Host", "RunContext"]

JSONValue: TypeAlias = "None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Context attribute keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in (bool, str, int, float):
        return value
    raise TypeError("Context attributes must contain JSON-like data")


@dataclass(frozen=True, slots=True)
class RunContext:
    """Host identity for one run. Only context_id is written to Wyrit's ledger."""

    context_id: str
    principal: str = field(repr=False)
    tenant: str = field(repr=False)
    attributes: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context_id, str) or not self.context_id.strip():
            raise ValueError("A context ID is required")
        if not isinstance(self.principal, str) or not isinstance(self.tenant, str):
            raise TypeError("Principal and tenant must be strings")
        if not isinstance(self.attributes, Mapping):
            raise TypeError("Context attributes must be a mapping")
        object.__setattr__(self, "attributes", _freeze(self.attributes))


class CapabilityError(Exception):
    """A dispatch failure with a code and an optional guest-visible reason."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        if code not in CALL_CODES:
            raise ValueError("Unsupported capability outcome code")
        if reason is not None and not isinstance(reason, str):
            raise TypeError("A capability error reason must be a string")
        super().__init__(code)
        self.code = code
        self.reason = reason


CatalogProvider: TypeAlias = Callable[[RunContext], Iterable[Capability]]
Dispatch: TypeAlias = Callable[[str, JSONValue, RunContext], Awaitable[JSONValue]]
LedgerSink: TypeAlias = Callable[[dict[str, Any]], None]


@dataclass
class _RunBinding:
    ids: Iterable[str]
    dispatch: Callable[..., Any] | None
    budgets: RunBudgets | None
    ledger_path: str | Path | None = None
    tracks_send: bool = False
    capture: RunCapture | None = None
    recipe: dict[str, Any] | None = None
    capabilities: dict[str, Capability] | None = None


class Host:
    """Execute disposable Wyrit runs against host-owned capabilities."""

    def __init__(
        self,
        capabilities: Iterable[Capability] | CatalogProvider | None = None,
        dispatch: Dispatch | None = None,
        *,
        catalog_provider: CatalogProvider | None = None,
        budgets: RunBudgets | None = None,
        recipes: str | Path | None = None,
        ledger_sink: LedgerSink | None = None,
    ) -> None:
        if callable(capabilities) and catalog_provider is None:
            catalog_provider, capabilities = capabilities, None
        if (capabilities is None) == (catalog_provider is None):
            raise TypeError("Supply capabilities or a catalog provider")
        if capabilities is not None:
            snapshot = tuple(capabilities)
            CapabilityRegistry(snapshot)

            def catalog_provider(_context: RunContext) -> Iterable[Capability]:
                return snapshot

        self._catalog_provider = catalog_provider
        self._dispatch = dispatch
        self._budgets = deepcopy(budgets)
        self._recipes_path = Path(recipes) if recipes is not None else None
        self._ledger_sink = ledger_sink

    def _catalog(self, context: RunContext) -> dict[str, Capability]:
        try:
            registry = CapabilityRegistry(self._catalog_provider(context))
            return {name: registry.resolve(name) for name in registry.ids}
        except Exception:
            raise ValueError("Could not load run catalog") from None

    def catalog_text(self, context: RunContext) -> str:
        """Render the visible capability catalog and runnable recipe summaries."""
        catalog = self._catalog(context)
        recipes = Recipes(self._recipes_path).entries(catalog, frozenset())
        return _recipe_description(_execute_description(catalog.values()), [
            recipe for recipe in recipes if recipe["state"] == "runnable"
        ])

    async def execute(self, source: str, context: RunContext) -> dict[str, Any]:
        """Execute a JavaScript async function body."""
        return await self._execute(source, context)

    async def run_recipe(
        self, name: str, arguments: dict[str, JSONValue], context: RunContext,
    ) -> dict[str, Any]:
        """Run an approved recipe with arguments validated against its schema."""
        if not isinstance(name, str):
            raise ConfigError("A recipe name is required.")
        return await self._execute("", context, name, arguments)

    async def _bind_run(
        self, context: RunContext, run_id: str, source: str, recipe_name: str | None,
    ) -> _RunBinding:
        def admit():
            catalog = self._catalog(context)
            recipe = None
            if recipe_name is not None:
                recipe = Recipes(self._recipes_path).get(recipe_name, catalog, frozenset())
                if recipe is None:
                    raise ConfigError("Recipe does not exist.")
                if recipe["state"] != "runnable":
                    reason = recipe["reason"] or "Approval is required."
                    raise ConfigError(f"Recipe is {recipe['state']}. {reason}")
            ids = catalog if recipe is None else recipe["capabilities"]
            return _RunBinding(tuple(ids), self._dispatch, self._budgets, recipe=recipe,
                               tracks_send=True, capabilities=catalog)

        return await to_thread.run_sync(admit)

    async def _execute(
        self, source: str, context: RunContext, name: str | None = None,
        arguments: dict[str, JSONValue] | None = None,
    ) -> dict[str, Any]:
        run_id = uuid4().hex
        binding = await self._bind_run(context, run_id, source, name)
        metadata = None
        if binding.recipe is not None:
            recipe = binding.recipe
            try:
                _check_json(arguments)
                _check_value(arguments)
                _validator(recipe["input_schema"]).validate(arguments)
            except (ValidationError, TypeError, ValueError, RecursionError):
                raise ConfigError("INVALID_ARGUMENTS: arguments do not match the recipe schema.") from None
            except Exception:
                raise ConfigError("INVALID_ARGUMENTS: recipe schema could not be evaluated.") from None
            source = recipe["source"]
            metadata = {"recipe": name, "source_digest": recipe["approval"]["source_digest"]}

        authorization = RunAuthorization(run_id, binding.ids)

        def run(dispatch, context_id):
            if binding.capture is not None:
                binding.capture.start(binding.ledger_path)
            return run_javascript(
                source, run_id=run_id, authorization=authorization, dispatch=dispatch,
                budgets=binding.budgets, ledger_path=binding.ledger_path,
                tracks_send=binding.tracks_send, _capture=binding.capture,
                recipe=metadata, arguments=arguments, ledger_sink=self._ledger_sink,
                context_id=context_id, local_echo=False,
                isolate_dispatch=binding.capabilities is not None,
            )

        if binding.capabilities is None:
            result = await to_thread.run_sync(run, binding.dispatch, context.context_id)
        else:
            async with BlockingPortal() as portal:
                def dispatch(capability_id, args, *, deadline, on_send):
                    capability = binding.capabilities[capability_id]
                    if capability.classification == "write":
                        raise CallError("DENIED", dispatched=False, audit_reason="WRITE_POLICY")
                    try:
                        _validator(capability.input_schema).validate(args)
                    except Exception:
                        raise CallError("INVALID_ARGUMENTS", dispatched=False) from None
                    value = portal.call(invoke, capability_id, args, deadline, on_send)
                    try:
                        _check_json(value)
                        _check_value(value, 1)
                        _validator(capability.output_schema).validate(value)
                    except Exception:
                        raise CallError("INVALID_RESULT", action_status="UNKNOWN") from None
                    return value

                async def invoke(capability_id, args, deadline, on_send):
                    with move_on_after(max(0, deadline - time.monotonic())):
                        on_send()
                        try:
                            return await binding.dispatch(capability_id, args, context)
                        except CapabilityError as error:
                            raise CallError(
                                error.code, dispatched=error.code != "DENIED",
                                public_reason=error.reason, audit_reason=error.reason, host_reported=True,
                            ) from None
                        except Exception:
                            raise CallError("TOOL_ERROR") from None
                    raise CallError("CALL_TIMEOUT")

                try:
                    route = dispatch if binding.dispatch is not None else None
                    result = await to_thread.run_sync(run, route, context.context_id)
                finally:
                    await portal.stop(cancel_remaining=True)

        response = asdict(result)
        return {
            "run_id": run_id, **response,
            "actions": list(response["actions"]),
            "unknown_calls": list(response["unknown_calls"]),
        }

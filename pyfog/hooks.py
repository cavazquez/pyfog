"""Declarative post-deploy hooks with retries, idempotency, and rollback."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn

from pydantic import Field, model_validator

from pyfog.plugins import PluginCall, PluginError, PluginRunner, redact_plugin_message, request_id
from pyfog.schemas import Schema


class HookError(RuntimeError):
    """A hook plan is invalid or could not be completed safely."""


class HookStep(Schema):
    """One idempotent plugin invocation in a post-deploy plan."""

    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9._-]*$")
    plugin: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9.-]*$")
    operation: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    capability: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")
    timeout_seconds: int = Field(default=30, ge=1, le=300, strict=True)
    retries: int = Field(default=0, ge=0, le=3, strict=True)
    rollback_plugin: str | None = Field(default=None, max_length=80, pattern=r"^[a-z][a-z0-9.-]*$")
    rollback_operation: str | None = Field(
        default=None, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$"
    )
    rollback_capability: str | None = Field(
        default=None, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$"
    )

    @model_validator(mode="after")
    def validate_rollback(self) -> HookStep:
        values = (self.rollback_plugin, self.rollback_operation, self.rollback_capability)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            msg = "El rollback debe declarar plugin, operación y capacidad."
            raise ValueError(msg)
        return self


class HookPlan(Schema):
    """Versioned set of hooks bound to one deployment operation."""

    api_version: int = Field(default=1, ge=1, le=1, strict=True)
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9._-]*$")
    steps: list[HookStep] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_steps(self) -> HookPlan:
        names = [step.name for step in self.steps]
        keys = [step.idempotency_key for step in self.steps]
        if len(set(names)) != len(names) or len(set(keys)) != len(keys):
            msg = "Los hooks deben tener nombres y claves de idempotencia únicos."
            raise ValueError(msg)
        return self


def _raise_plugin_error(message: str) -> NoReturn:
    raise PluginError(message)


def _raise_hook_failure(step: HookStep, attempts: int, last_error: PluginError | None) -> NoReturn:
    msg = (
        f"El hook {step.name} falló tras {attempts} intento(s): "
        f"{redact_plugin_message(str(last_error or 'error desconocido'))}"
    )
    raise HookError(msg)


@dataclass(frozen=True)
class HookExecution:
    name: str
    idempotency_key: str
    attempts: int
    response: dict[str, Any]


@dataclass(frozen=True)
class HookRunResult:
    completed: tuple[HookExecution, ...]
    rolled_back: tuple[str, ...]


@dataclass
class HookLedger:
    """Durable-store adapter boundary; callers can persist keys after each success."""

    completed_keys: set[str] = field(default_factory=set)

    def contains(self, key: str) -> bool:
        return key in self.completed_keys

    def mark(self, key: str) -> None:
        self.completed_keys.add(key)


def run_post_deploy_hooks(
    plan: HookPlan,
    runner: PluginRunner,
    *,
    context: Mapping[str, Any],
    ledger: HookLedger | None = None,
    secret: bytes | None = None,
) -> HookRunResult:
    """Run hooks in order and compensate completed work in reverse order on failure.

    The ledger is updated immediately after every successful hook.  A retry of the same task can
    therefore skip an already completed hook without asking the integration to repeat a side
    effect.  The caller should persist the ledger transactionally with its task event.
    """

    state = ledger if ledger is not None else HookLedger()
    completed: list[tuple[HookStep, HookExecution]] = []
    try:
        for step in plan.steps:
            if state.contains(step.idempotency_key):
                continue
            payload = dict(context)
            payload.update(step.payload)
            attempts = 0
            response: dict[str, Any] | None = None
            last_error: PluginError | None = None
            for _attempts in range(1, step.retries + 2):
                try:
                    result = runner.run(
                        step.plugin,
                        PluginCall(
                            operation=step.operation,
                            payload=payload,
                            secret=secret,
                            request_id=request_id(),
                            required_capability=step.capability,
                            timeout_seconds=step.timeout_seconds,
                        ),
                    )
                    if not result.success:
                        _raise_plugin_error(result.error)
                    response = result.result
                    break
                except PluginError as error:
                    last_error = error
            attempts = _attempts
            if response is None:
                _raise_hook_failure(step, attempts, last_error)
            execution = HookExecution(
                name=step.name,
                idempotency_key=step.idempotency_key,
                attempts=attempts,
                response=response,
            )
            state.mark(step.idempotency_key)
            completed.append((step, execution))
    except HookError:
        rolled_back: list[str] = []
        for step, _execution in reversed(completed):
            if step.rollback_plugin is None:
                continue
            try:
                rollback = runner.run(
                    step.rollback_plugin,
                    PluginCall(
                        operation=step.rollback_operation or "rollback",
                        payload={**context, "idempotency_key": step.idempotency_key},
                        secret=secret,
                        request_id=request_id(),
                        required_capability=step.rollback_capability,
                        timeout_seconds=step.timeout_seconds,
                    ),
                )
                if rollback.success:
                    rolled_back.append(step.name)
            except PluginError:
                # The original failure remains the actionable error; the caller can retry rollback
                # from the persisted completed-key ledger without exposing plugin output.
                continue
        raise HookError(
            "La ejecución post-deploy falló; "
            + (
                "se ejecutó rollback para " + ", ".join(rolled_back)
                if rolled_back
                else "requiere rollback"
            )
        ) from None
    return HookRunResult(
        completed=tuple(execution for _step, execution in completed),
        rolled_back=(),
    )

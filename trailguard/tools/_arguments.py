"""Argument coercion for the tool bodies.

Why this exists
---------------
The harness validates a tool call against its declared input model and then
invokes the implementation with the *raw* argument mapping (spec 15)::

    input_model.model_validate(arguments)   # validate, raise on bad input
    result = func(**arguments)              # call with what came in

So a tool body receives whatever the caller handed over -- for the agent path
that is JSON-shaped data, i.e. ``{"current_position": {"lat": ...}}`` with plain
dicts and ISO8601 strings, not models. A body written as
``current_position.lat`` would therefore raise ``AttributeError: 'dict' object has
no attribute 'lat'`` *after* the contract had already said the input was valid,
which is the worst possible failure mode: the caller is told the input is fine
and then told the call broke.

Split ownership
---------------
Validation stays where it belongs -- in the declared input model, invoked by the
harness. This module adds the missing second half: re-binding the values the
model accepts into the model instances the body expects, so a body never
performs its own ad-hoc dict unwrapping and a Python caller may still pass models
directly.

How it coerces
--------------
Field by field, through the declared field's own annotation, using pydantic's
:class:`~pydantic.TypeAdapter`. That is deliberate: validating the whole mapping
at once would demand *every* required field be present in this one call, but the
harness and a direct Python caller each already supplied some of them elsewhere,
and a missing field is the contract's error to raise with its own message, not
this module's. Each adapter is built once per field and cached, because the
annotations are fixed at import time.

Honest limits
-------------
The decorator is *not* a validator and adds no rules of its own: it coerces only
the fields the declared model owns (keyword-only extras a body accepts that the
model does not know about pass through untouched), it never invents a default
for a missing argument, and it reads no clock, so determinism is unaffected
(spec 19).
"""

from __future__ import annotations

import inspect
from functools import lru_cache, wraps
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, TypeAdapter

__all__ = ["binds_input_contract"]

F = TypeVar("F", bound=Callable[..., Any])


def binds_input_contract(model: type[BaseModel]) -> Callable[[F], F]:
    """Bind a tool body's arguments to its declared input contract.

    Args:
        model: The pydantic model that declares the tool's arguments (e.g.
            :class:`~trailguard.tools.GetRouteContextInput`). It is the sole
            source of truth for which arguments are contract fields and what
            shape they must take.

    Returns:
        A decorator that, before calling the body, replaces every contract field
        that arrived as raw data with the validated model instance.

    A value that is already correct -- a model instance, or ``None`` for a field
    the caller declined to supply -- passes through untouched, so a direct Python
    caller that already built ``RouteContext(...)`` is never re-validated.
    """

    declared_fields = set(model.model_fields)

    def decorator(func: F) -> F:
        signature = inspect.signature(func)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            payload = {
                name: value
                for name, value in bound.arguments.items()
                if name in declared_fields and _needs_coercion(value)
            }
            for name, value in payload.items():
                bound.arguments[name] = _coerce_field(model, name, value)
            return func(*bound.args, **bound.kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def _needs_coercion(value: Any) -> bool:
    """Whether ``value`` is raw data that still needs to become a model.

    ``None`` is left alone: a field the caller declined to supply must stay
    ``None`` rather than being turned into something, and a *required* field that
    is missing entirely never reaches this function -- the contract's own error
    names it.
    """
    return value is not None and not isinstance(value, BaseModel)


@lru_cache(maxsize=None)
def _adapter_for(model: type[BaseModel], field_name: str) -> TypeAdapter:
    """The :class:`TypeAdapter` for one declared field, built once.

    Cached because the set of (model, field) pairs is fixed at import time and a
    horizon loop performs the same coercion four times over.
    """
    field = model.model_fields[field_name]
    return TypeAdapter(field.annotation)


def _coerce_field(model: type[BaseModel], field_name: str, value: Any) -> Any:
    """Validate one raw field value against its declared annotation.

    Raises:
        pydantic.ValidationError: the value does not satisfy the field. The
            message is the contract's own, so an agent path and a Python caller
            are told the same thing.
    """
    return _adapter_for(model, field_name).validate_python(value)

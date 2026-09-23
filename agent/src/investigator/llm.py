"""One model for every LLM node (see DESIGN.md, Decisions).

Nodes depend on the small `StructuredLLM` interface, so tests and offline runs
can swap in `ScriptedLLM` without touching the graph.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, TypeVar

from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class StructuredLLM(Protocol):
    """Implementations may also set `last_usage` ({"input_tokens", "output_tokens"})
    and `model_name` after each call; budgets and telemetry read them if present."""

    def structured(self, schema: type[T], system: str, user: str) -> tuple[T, float]:
        """Return a parsed instance of `schema` and the call's cost in USD."""


class LangChainLLM:
    """Any chat model LangChain's `init_chat_model` supports, with structured output.

    Prices are per million tokens and only used for the budget; leave them at
    0 to disable the cost limit in practice.
    """

    def __init__(
        self,
        model: str,
        provider: Optional[str] = None,
        input_usd_per_mtok: float = 0.0,
        output_usd_per_mtok: float = 0.0,
        temperature: Optional[float] = 0.0,
        max_retries: int = 2,
    ) -> None:
        from langchain.chat_models import init_chat_model

        kwargs = {"model_provider": provider} if provider else {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        self.model = init_chat_model(model, **kwargs)
        self.input_price = input_usd_per_mtok
        self.output_price = output_usd_per_mtok
        self.max_retries = max_retries
        self.model_name = model
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}

    def structured(self, schema, system, user):
        from langchain_core.messages import HumanMessage, SystemMessage

        runnable = self.model.with_structured_output(schema, include_raw=True)
        cost = 0.0
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        last_error: object = None
        for attempt in range(self.max_retries + 1):
            out = runnable.invoke([SystemMessage(system), HumanMessage(user)])
            usage = getattr(out.get("raw"), "usage_metadata", None) or {}
            for k in self.last_usage:
                self.last_usage[k] += usage.get(k, 0)
            cost += (
                usage.get("input_tokens", 0) * self.input_price
                + usage.get("output_tokens", 0) * self.output_price
            ) / 1_000_000
            if out.get("parsed") is not None:
                return out["parsed"], cost
            last_error = out.get("parsing_error")
            log.warning("structured output for %s failed (attempt %d): %s", schema.__name__, attempt + 1, last_error)
        raise LLMError(f"model returned no valid {schema.__name__}: {last_error}")


class ScriptedLLM:
    """Returns pre-written responses in order, keyed by output schema name.

    Used by tests and by `investigate --scripted` to replay the case study
    without a model. Each entry is a dict validated against the schema.
    """

    def __init__(self, script: dict[str, list[dict]], cost_per_call: float = 0.0,
                 input_tokens_per_call: int = 0, output_tokens_per_call: int = 0) -> None:
        self.script = {k: list(v) for k, v in script.items() if not k.startswith("_")}
        self.cost_per_call = cost_per_call
        self.tokens = {"input_tokens": input_tokens_per_call, "output_tokens": output_tokens_per_call}
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        self.model_name = "scripted"
        self.calls: list[tuple[str, str]] = []

    @classmethod
    def from_file_data(cls, data: dict) -> "ScriptedLLM":
        """Script files may carry clearly labelled simulated cost and token
        figures, for demonstrating budgets without a model."""
        sim = data.get("_simulated", {})
        return cls(data, cost_per_call=sim.get("cost_per_call", 0.0),
                   input_tokens_per_call=sim.get("input_tokens_per_call", 0),
                   output_tokens_per_call=sim.get("output_tokens_per_call", 0))

    def structured(self, schema, system, user):
        self.calls.append((schema.__name__, user))
        self.last_usage = dict(self.tokens)
        queue = self.script.get(schema.__name__, [])
        if not queue:
            raise LLMError(f"script has no more {schema.__name__} responses")
        return schema.model_validate(queue.pop(0)), self.cost_per_call

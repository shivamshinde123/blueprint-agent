"""Provider-agnostic LLM client, wired to OpenRouter by default.

Why OpenRouter: one key and one interface for every model, so comparing
candidates for the discovery loop is a config change rather than a rewrite.
Because the endpoint is OpenAI-compatible, pointing ``BLUEPRINT_LLM_BASE_URL``
at any other compatible gateway (or a local model) also works — nothing else in
the codebase imports a provider SDK.

Two behaviours here matter to the rest of the system:

**Structured decisions.** The discovery loop needs decisions that are valid by
construction; a malformed JSON blob mid-run means either a crash or a repair
heuristic that quietly guesses. :meth:`LLMClient.decide` uses JSON-schema
structured outputs derived from the caller's Pydantic model, so the response
either validates or raises.

**Pinned routing.** OpenRouter can transparently fall back to a different
upstream provider. For a system whose whole thesis is deterministic replay,
silent provider substitution mid-run is exactly the wrong failure mode, so
routing is pinned and fallbacks are disabled by default
(``BLUEPRINT_ALLOW_PROVIDER_FALLBACK=1`` opts back in).

Note that sampling parameters are deliberately never sent: current Claude
models reject ``temperature`` / ``top_p`` / ``seed`` outright, and OpenRouter's
own capability listing for ``anthropic/claude-sonnet-5`` omits them. Determinism
in this system is structural — it lives in the artifact and the LLM-free replay
path, not in the sampler. See PLAN.md §11 C3.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, TypeVar

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from src import settings

log = logging.getLogger(__name__)

TDecision = TypeVar("TDecision", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised for any unrecoverable problem talking to the model."""


@dataclass(slots=True)
class Usage:
    """Token accounting for one call, recorded in the evidence log."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    #: OpenRouter's generation id. Useful for after-the-fact cost lookups.
    generation_id: str | None = None
    #: Which upstream provider actually served the request.
    provider: str | None = None
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class Decision[T: BaseModel]:
    """A validated model decision plus the metadata the evidence log wants."""

    value: T
    usage: Usage
    raw_text: str = ""


@dataclass(slots=True)
class LLMClient:
    """Thin wrapper over an OpenAI-compatible chat completions endpoint."""

    model: str = field(default_factory=lambda: settings.MODEL_SLUG)
    base_url: str = field(default_factory=lambda: settings.LLM_BASE_URL)
    api_key: str | None = None
    effort: str = field(default_factory=lambda: settings.DISCOVERY_EFFORT)
    max_tokens: int = settings.MAX_TOKENS
    timeout_s: float = 120.0
    max_retries: int = 2
    _client: OpenAI | None = field(default=None, init=False, repr=False)
    #: Accumulated across the run so the evidence log can report total spend.
    calls: list[Usage] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        key = self.api_key or settings.llm_api_key()
        if not key:
            raise LLMError(
                f"no API key found. Set {settings.API_KEY_ENV} in .env "
                f"(get one at https://openrouter.ai/keys)."
            )
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=key,
            timeout=self.timeout_s,
            max_retries=self.max_retries,
            # Optional attribution headers; harmless against other gateways.
            default_headers={
                "HTTP-Referer": settings.PROJECT_URL,
                "X-Title": settings.PROJECT_NAME,
            },
        )

    # -- public API --------------------------------------------------------

    def decide(
        self,
        *,
        system: str,
        user: str,
        schema: type[TDecision],
        image_png: bytes | None = None,
        cache_system: bool = True,
    ) -> Decision[TDecision]:
        """Ask for one decision and return it validated against *schema*.

        ``image_png`` attaches a screenshot for the Layer 2 vision fallback.
        ``cache_system`` marks the system prompt for provider-side prompt
        caching — it carries the full artifact spec, is byte-stable across the
        run, and is billed at roughly a tenth of the normal input rate on a hit.
        """
        messages = [
            self._system_message(system, cache=cache_system),
            self._user_message(user, image_png),
        ]

        response = self._create(
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _json_schema_for(schema),
                },
            },
        )

        choice = response.choices[0]
        text = choice.message.content or ""
        if not text.strip():
            if choice.finish_reason == "length":
                raise LLMError(
                    f"the model hit the {self.max_tokens} token output cap "
                    f"before answering. Reasoning tokens count against this "
                    f"budget, so a high-effort decision can spend the whole cap "
                    f"deliberating. Raise BLUEPRINT_MAX_TOKENS, or lower "
                    f"BLUEPRINT_EFFORT from {self.effort!r}."
                )
            raise LLMError(
                f"model returned an empty response "
                f"(finish_reason={choice.finish_reason!r}); nothing to decide on"
            )

        try:
            value = schema.model_validate_json(text)
        except ValidationError as exc:
            # Structured outputs should make this unreachable. If it fires, the
            # gateway silently downgraded the request rather than honouring the
            # schema -- worth failing loudly instead of guessing at a repair.
            raise LLMError(
                f"model response did not match {schema.__name__} despite "
                f"structured outputs being requested:\n{exc}\n\nraw: {text[:800]}"
            ) from exc

        usage = self._usage_from(response)
        self.calls.append(usage)
        return Decision(value=value, usage=usage, raw_text=text)

    def total_usage(self) -> Usage:
        """Aggregate of every call made through this client."""
        total = Usage(model=self.model)
        for call in self.calls:
            total.prompt_tokens += call.prompt_tokens
            total.completion_tokens += call.completion_tokens
            total.cached_tokens += call.cached_tokens
        return total

    @property
    def call_count(self) -> int:
        """Number of model calls made. Replay asserts this is 0 in strict mode."""
        return len(self.calls)

    # -- internals ---------------------------------------------------------

    def _create(self, **kwargs: Any) -> Any:
        extra_body: dict[str, Any] = {
            # Reasoning depth. OpenRouter maps this onto each provider's own
            # mechanism (adaptive thinking for Claude).
            "reasoning": {"effort": self.effort},
        }
        if settings.PINNED_PROVIDERS:
            extra_body["provider"] = {
                "order": list(settings.PINNED_PROVIDERS),
                "allow_fallbacks": settings.ALLOW_PROVIDER_FALLBACK,
            }

        try:
            return self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=self.max_tokens,
                # No temperature/top_p/seed: rejected by current Claude models,
                # and determinism here is structural anyway (PLAN.md C3).
                extra_body=extra_body,
                **kwargs,
            )
        except AuthenticationError as exc:
            raise LLMError(
                f"authentication failed - check {settings.API_KEY_ENV} in .env"
            ) from exc
        except BadRequestError as exc:
            raise LLMError(
                f"request rejected for model {self.model!r}: {exc}. "
                f"If this mentions an unsupported parameter, the model may not "
                f"support structured outputs - check its capabilities at "
                f"https://openrouter.ai/models"
            ) from exc
        except RateLimitError as exc:
            raise LLMError(f"rate limited by the gateway: {exc}") from exc
        except APIConnectionError as exc:
            raise LLMError(f"could not reach {self.base_url}: {exc}") from exc
        except APIStatusError as exc:
            raise LLMError(f"gateway error {exc.status_code}: {exc}") from exc

    @staticmethod
    def _system_message(text: str, *, cache: bool) -> dict[str, Any]:
        if not cache:
            return {"role": "system", "content": text}
        # Anthropic-style cache breakpoint, passed through by OpenRouter.
        # Ignored harmlessly by gateways that do not implement it.
        return {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }

    @staticmethod
    def _user_message(text: str, image_png: bytes | None) -> dict[str, Any]:
        if image_png is None:
            return {"role": "user", "content": text}
        encoded = base64.standard_b64encode(image_png).decode("ascii")
        return {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                },
                {"type": "text", "text": text},
            ],
        }

    @staticmethod
    def _usage_from(response: Any) -> Usage:
        raw = getattr(response, "usage", None)
        details = getattr(raw, "prompt_tokens_details", None)
        return Usage(
            prompt_tokens=getattr(raw, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(raw, "completion_tokens", 0) or 0,
            cached_tokens=getattr(details, "cached_tokens", 0) or 0,
            generation_id=getattr(response, "id", None),
            provider=getattr(response, "provider", None),
            model=getattr(response, "model", None),
        )


# --------------------------------------------------------------------------
# Schema conversion
# --------------------------------------------------------------------------


def _json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Produce a strict-mode JSON schema from a Pydantic model.

    Strict structured outputs require every property to be listed in
    ``required`` and ``additionalProperties: false`` on every object. Pydantic
    marks fields with defaults as optional, so we tighten the result rather
    than hand-maintaining a parallel schema.
    """
    schema = model.model_json_schema()
    _inline_refs(schema, schema.get("$defs", {}))
    schema.pop("$defs", None)
    _tighten(schema)
    return schema


def _inline_refs(node: Any, defs: dict[str, Any], depth: int = 0) -> None:
    """Replace ``$ref`` pointers with their definitions, in place."""
    if depth > 50:
        raise LLMError("schema nesting too deep to inline (possible cycle)")
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1])
            if target is not None:
                node.pop("$ref")
                merged = json.loads(json.dumps(target))
                _inline_refs(merged, defs, depth + 1)
                for key, value in merged.items():
                    node.setdefault(key, value)
        for value in node.values():
            _inline_refs(value, defs, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _inline_refs(item, defs, depth + 1)


def _tighten(node: Any) -> None:
    """Enforce strict-mode object rules recursively, in place."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        # Strict mode rejects these annotation-only keywords.
        for key in ("default", "examples", "$comment"):
            node.pop(key, None)
        for value in node.values():
            _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)

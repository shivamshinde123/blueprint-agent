"""LLM access layer.

The rest of the codebase talks to :class:`~src.llm.client.LLMClient` and never
imports a provider SDK directly. Swapping providers is a base-URL and model-slug
change, not a refactor.
"""

from src.llm.client import Decision, LLMClient, LLMError, Usage

__all__ = ["Decision", "LLMClient", "LLMError", "Usage"]

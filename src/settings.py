"""Project-wide settings and paths.

Anything a reviewer might want to change lives here, not scattered through the
modules.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = ROOT / "artifacts"
HEAL_DIR = ARTIFACTS_DIR / "heal"
EVIDENCE_DIR = ROOT / "evidence"
SCREENSHOTS_DIR = EVIDENCE_DIR / "screenshots"
CONFIG_DIR = ROOT / "config"
ALLOWLIST_PATH = CONFIG_DIR / "allowlist.json"
MOCK_DIR = ROOT / "mock"

# --------------------------------------------------------------------------
# Project identity (sent as optional attribution headers)
# --------------------------------------------------------------------------

PROJECT_NAME = "Blueprint Agent"
PROJECT_URL = "https://github.com/shivamshinde123/blueprint-agent"

# --------------------------------------------------------------------------
# Model access — OpenRouter by default
# --------------------------------------------------------------------------
#
# Access goes through an OpenAI-compatible gateway so that switching models is
# a config change. Point BLUEPRINT_LLM_BASE_URL elsewhere (another gateway, a
# local server) and nothing else in the codebase changes.
#
# One model serves both the accessibility-tree text loop and the screenshot
# vision fallback, so discovery needs exactly one slug.

LLM_BASE_URL = os.getenv("BLUEPRINT_LLM_BASE_URL", "https://openrouter.ai/api/v1")

#: OpenRouter-style ``vendor/model`` slug. Browse at https://openrouter.ai/models
MODEL_SLUG = os.getenv("BLUEPRINT_MODEL", "anthropic/claude-sonnet-5")

#: Env var holding the gateway key.
API_KEY_ENV = os.getenv("BLUEPRINT_API_KEY_ENV", "OPENROUTER_API_KEY")

#: Upstream providers to route to, most preferred first. Pinning matters here:
#: a gateway silently substituting a different provider mid-run is the wrong
#: failure mode for a system built around reproducibility. Empty list = let the
#: gateway choose.
PINNED_PROVIDERS: tuple[str, ...] = tuple(
    p.strip() for p in os.getenv("BLUEPRINT_PROVIDERS", "anthropic").split(",") if p.strip()
)

#: Opt back into gateway fallback when the pinned provider is unavailable.
ALLOW_PROVIDER_FALLBACK = os.getenv("BLUEPRINT_ALLOW_PROVIDER_FALLBACK", "0") == "1"

#: Reasoning depth for discovery decisions: low | medium | high | xhigh | max.
DISCOVERY_EFFORT = os.getenv("BLUEPRINT_EFFORT", "high")

# Reasoning tokens count against this budget, so a high-effort decision can
# exhaust a 16k cap before emitting any answer -- which surfaces as an empty
# response with finish_reason "length", not as an obvious error.
MAX_TOKENS = int(os.getenv("BLUEPRINT_MAX_TOKENS", "32000"))

#: Sampling parameters are deliberately absent. Current Claude models reject
#: temperature/top_p/seed, and determinism here is structural: it lives in the
#: artifact and the LLM-free replay path, not in the sampler. See PLAN.md C3.

# --------------------------------------------------------------------------
# Discovery limits (PLAN.md §5.2)
# --------------------------------------------------------------------------

# Generous enough for a real flow that needs a few corrections. An eight-step
# task can legitimately take fifteen turns when a typeahead has to be
# re-selected or a search re-run; the dead-end detector, not this cap, is what
# stops a genuinely stuck agent.
MAX_DISCOVERY_STEPS = 40
DISCOVERY_TIMEOUT_S = 600
DEAD_END_THRESHOLD = 3

# --------------------------------------------------------------------------
# Operator console (PLAN.md §8.4)
# --------------------------------------------------------------------------

OPERATOR_HOST = os.getenv("BLUEPRINT_OPERATOR_HOST", "127.0.0.1")
OPERATOR_PORT = int(os.getenv("BLUEPRINT_OPERATOR_PORT", "8080"))
MOCK_PORT = int(os.getenv("BLUEPRINT_MOCK_PORT", "8081"))


def operator_url(session_id: str) -> str:
    return f"http://{OPERATOR_HOST}:{OPERATOR_PORT}/operator?session_id={session_id}"


# --------------------------------------------------------------------------
# Credentials (read from .env; never logged, never written to an artifact)
# --------------------------------------------------------------------------


def llm_api_key() -> str | None:
    """Key for the model gateway, from whichever env var ``API_KEY_ENV`` names."""
    return os.getenv(API_KEY_ENV) or None


def credentials(prefix: str) -> dict[str, str | None]:
    """Return ``{username, password}`` for a target app prefix, e.g. ``ORANGEHRM``."""
    return {
        "username": os.getenv(f"{prefix}_USERNAME"),
        "password": os.getenv(f"{prefix}_PASSWORD"),
    }


def ensure_dirs() -> None:
    for path in (ARTIFACTS_DIR, HEAL_DIR, EVIDENCE_DIR, SCREENSHOTS_DIR):
        path.mkdir(parents=True, exist_ok=True)

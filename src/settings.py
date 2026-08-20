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
# Model
# --------------------------------------------------------------------------

#: Handles both the accessibility-tree text loop and the screenshot vision
#: fallback, so discovery needs exactly one model.
#:
#: Note: sampling parameters (temperature/top_p/top_k) are rejected by current
#: models. Determinism in this system is structural — it comes from the
#: artifact and the LLM-free replay path, not from pinning the sampler.
#: See PLAN.md §11 C3.
MODEL_ID = os.getenv("BLUEPRINT_MODEL", "claude-sonnet-5")

#: Reasoning depth for discovery decisions: low | medium | high | xhigh | max.
DISCOVERY_EFFORT = os.getenv("BLUEPRINT_EFFORT", "high")

MAX_TOKENS = 16_000

# --------------------------------------------------------------------------
# Discovery limits (PLAN.md §5.2)
# --------------------------------------------------------------------------

MAX_DISCOVERY_STEPS = 25
DISCOVERY_TIMEOUT_S = 300
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


def anthropic_api_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY") or None


def credentials(prefix: str) -> dict[str, str | None]:
    """Return ``{username, password}`` for a target app prefix, e.g. ``ORANGEHRM``."""
    return {
        "username": os.getenv(f"{prefix}_USERNAME"),
        "password": os.getenv(f"{prefix}_PASSWORD"),
    }


def ensure_dirs() -> None:
    for path in (ARTIFACTS_DIR, HEAL_DIR, EVIDENCE_DIR, SCREENSHOTS_DIR):
        path.mkdir(parents=True, exist_ok=True)

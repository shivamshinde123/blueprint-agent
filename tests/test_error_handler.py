"""Error classification, retry accounting, and exhaustion.

No browser. This is the logic that decides whether "member not found" is an
answer, a retry, or a crash.
"""

from __future__ import annotations

import pytest

from src.artifact.schema import Artifact, ErrorTypeKey
from src.replay.error_handler import (
    Disposition,
    ErrorTracker,
    EscalateToHuman,
    ReplayFailure,
    classify_exception,
)


@pytest.fixture
def artifact(artifact_dict) -> Artifact:
    return Artifact.model_validate(artifact_dict)


@pytest.fixture
def tracker(artifact) -> ErrorTracker:
    return ErrorTracker(artifact)


# --------------------------------------------------------------------------
# Retry accounting
# --------------------------------------------------------------------------


def test_recoverable_error_retries_up_to_its_limit(tracker):
    # _default gives element_not_found max_retries=3.
    for attempt in range(1, 4):
        verdict = tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
        assert verdict.disposition is Disposition.RETRY
        assert verdict.attempt == attempt


def test_exhausted_retries_escalate_when_configured(tracker):
    for _ in range(3):
        tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    verdict = tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    assert verdict.disposition is Disposition.ESCALATE
    assert verdict.capture_screenshot


def test_exhausted_retries_fail_when_configured(tracker):
    # checkpoint_failed has on_exhausted=hard_failure, max_retries=2.
    for _ in range(2):
        tracker.record(4, ErrorTypeKey.CHECKPOINT_FAILED)
    verdict = tracker.record(4, ErrorTypeKey.CHECKPOINT_FAILED)
    assert verdict.disposition is Disposition.FAIL


def test_counters_are_per_step(tracker):
    tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    assert tracker.attempts_for(2, ErrorTypeKey.ELEMENT_NOT_FOUND) == 2
    assert tracker.attempts_for(5, ErrorTypeKey.ELEMENT_NOT_FOUND) == 0


def test_counters_are_per_error_type(tracker):
    tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    assert tracker.attempts_for(2, ErrorTypeKey.TIMEOUT) == 0


def test_success_resets_the_step(tracker):
    """A step that recovers must not carry its history into a later re-entry,
    e.g. after session recovery restarts it."""
    tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND)
    tracker.reset_step(2)
    assert tracker.attempts_for(2, ErrorTypeKey.ELEMENT_NOT_FOUND) == 0
    assert tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND).disposition is Disposition.RETRY


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


def test_hard_failure_stops_immediately(tracker):
    """step_8 declares extraction_empty as a hard failure."""
    verdict = tracker.record(8, ErrorTypeKey.EXTRACTION_EMPTY)
    assert verdict.disposition is Disposition.FAIL
    assert verdict.attempt == 1
    assert verdict.capture_screenshot


def test_session_expiry_triggers_re_login(tracker):
    verdict = tracker.record(5, ErrorTypeKey.SESSION_EXPIRED)
    assert verdict.disposition is Disposition.RE_LOGIN


def test_per_step_entry_beats_default(artifact_dict):
    artifact_dict["error_map"]["step_5"] = {
        "element_not_found": {
            "category": "hard_failure",
            "action": "stop",
            "capture_screenshot": True,
            "message": "step 5 is not worth retrying",
        }
    }
    tracker = ErrorTracker(Artifact.model_validate(artifact_dict))
    assert tracker.record(5, ErrorTypeKey.ELEMENT_NOT_FOUND).disposition is Disposition.FAIL
    # Other steps still get the retrying default.
    assert tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND).disposition is Disposition.RETRY


def test_unclassified_error_fails_rather_than_continuing(artifact_dict):
    """Silently continuing past an unclassified error is how a replay
    'succeeds' having done the wrong thing."""
    artifact_dict["error_map"] = {}
    tracker = ErrorTracker(Artifact.model_validate(artifact_dict))
    verdict = tracker.record(3, ErrorTypeKey.TIMEOUT)
    assert verdict.disposition is Disposition.FAIL
    assert "no error_map entry" in verdict.message


def test_global_default_used_when_handler_omits_limits(artifact_dict):
    artifact_dict["error_map"]["_default"]["element_not_found"].pop("max_retries")
    artifact_dict["replay_config"]["max_retries_per_step"] = 1
    tracker = ErrorTracker(Artifact.model_validate(artifact_dict))
    assert tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND).disposition is Disposition.RETRY
    assert tracker.record(2, ErrorTypeKey.ELEMENT_NOT_FOUND).disposition is Disposition.ESCALATE


# --------------------------------------------------------------------------
# Exception classification
# --------------------------------------------------------------------------


def test_element_not_found_is_classified():
    from src.replay.locator import ElementNotFound

    assert classify_exception(ElementNotFound("nope")) is ErrorTypeKey.ELEMENT_NOT_FOUND


def test_budget_exhaustion_classifies_as_element_not_found():
    from src.replay.locator import LayerBudgetExhausted

    assert (
        classify_exception(LayerBudgetExhausted("spent"))
        is ErrorTypeKey.ELEMENT_NOT_FOUND
    )


def test_timeout_is_classified_by_message():
    assert classify_exception(RuntimeError("Timeout 8000ms exceeded")) is ErrorTypeKey.TIMEOUT


def test_unknown_exception_falls_back_to_wrong_page_state():
    assert classify_exception(ValueError("something odd")) is ErrorTypeKey.WRONG_PAGE_STATE


# --------------------------------------------------------------------------
# Failure reporting
# --------------------------------------------------------------------------


def test_replay_failure_carries_expected_and_observed():
    failure = ReplayFailure(
        step_id=7,
        error_type=ErrorTypeKey.CHECKPOINT_FAILED,
        message="results never appeared",
        expected="page contains 'Records Found'",
        observed="page still showed the search form",
    )
    data = failure.as_dict()
    assert data["failed_at_step"] == 7
    assert data["expected"] and data["observed"]


def test_escalation_carries_the_step_it_stopped_at():
    exc = EscalateToHuman("stuck", 5)
    assert exc.step_id == 5
    assert "stuck" in str(exc)

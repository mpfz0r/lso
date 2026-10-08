"""Result records kept in the job store for changed, failed and unreachable hosts."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from lso.routes.playbook import PlaybookJobStatus
from lso.tasks import (
    _register_finished_job,
    _register_running_job,
    get_job_status,
    playbook_event_handler_factory,
)

DIFF = {"before": {"data": []}, "after": {"data": [{"name": "peer-1"}]}}


def _event(name: str, res: dict[str, Any] | None = None, stdout: str = "line", **data: Any) -> dict[str, Any]:
    return {
        "event": name,
        "stdout": stdout,
        "event_data": {"host": "vpn-core-1", "task": "Configure peers", "res": res or {}, **data},
    }


def _handler(job_id: str) -> Callable[[dict[str, Any]], bool]:
    _register_running_job(job_id)
    return playbook_event_handler_factory(job_id, None, progress_is_incremental=True)


def _results(job_id: str) -> list[dict[str, Any]]:
    status = get_job_status(job_id)
    assert status is not None
    return status["results"]


def test_changed_result_is_recorded_with_its_diff() -> None:
    handler = _handler("job-changed")
    handler(_event("runner_on_ok", res={"changed": True, "diff": DIFF}))

    assert _results("job-changed") == [
        {
            "host": "vpn-core-1",
            "task": "Configure peers",
            "changed": True,
            "failed": False,
            "unreachable": False,
            "diff": DIFF,
        }
    ]


def test_unchanged_ok_result_is_not_recorded() -> None:
    handler = _handler("job-unchanged")
    handler(_event("runner_on_ok", res={"changed": False}))

    assert _results("job-unchanged") == []


def test_failed_and_unreachable_results_are_recorded() -> None:
    handler = _handler("job-failed")
    handler(_event("runner_on_failed", res={"msg": "boom"}))
    handler(_event("runner_on_unreachable", res={"msg": "timeout"}))

    assert [(r["failed"], r["unreachable"]) for r in _results("job-failed")] == [(True, False), (False, True)]


def test_ignored_failure_is_not_recorded() -> None:
    handler = _handler("job-ignored")
    handler(_event("runner_on_failed", res={"msg": "boom"}, ignore_errors=True))

    assert _results("job-ignored") == []


def test_result_without_stdout_is_still_recorded() -> None:
    handler = _handler("job-quiet")
    handler(_event("runner_on_ok", res={"changed": True}, stdout=""))

    assert len(_results("job-quiet")) == 1


def test_results_survive_job_completion_and_reach_the_response_model() -> None:
    handler = _handler("job-done")
    handler(_event("runner_on_ok", res={"changed": True, "diff": DIFF}))
    runner = SimpleNamespace(status="successful", rc=0, stats={}, stdout=None)
    _register_finished_job("job-done", runner)  # type: ignore[arg-type]

    status = get_job_status("job-done")
    assert status is not None
    assert PlaybookJobStatus(job_id=uuid4(), **status).results[0]["diff"] == DIFF

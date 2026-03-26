# Copyright 2024-2025 GÉANT Vereniging.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module defines tasks for executing Ansible playbooks asynchronously using Celery.

The primary task, `run_playbook_proc_task`, runs an Ansible playbook and sends a POST request with
the results to a specified callback URL.
"""

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import requests
from ansible_runner import Runner, run
from starlette import status

from lso.config import settings
from lso.schema import ExecutableRunResponse, InventoryFile

logger = logging.getLogger(__name__)

#: In-memory store of playbook job results, keyed by ``job_id``.
_job_store: dict[str, dict[str, Any]] = {}
_job_store_lock = threading.Lock()


def _register_running_job(job_id: str) -> None:
    """Mark a job as running before the playbook starts."""
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "running",
            "rc": None,
            "stdout": [],
            "stats": {},
        }


def _append_stdout(job_id: str, lines: list[str]) -> None:
    """Append new stdout lines to a running job's store entry.

    Called from the ansible-runner event handler so that poll clients
    see incremental output while the playbook is still running.
    """
    with _job_store_lock:
        entry = _job_store.get(job_id)
        if entry is not None:
            entry["stdout"].extend(lines)


def _register_finished_job(job_id: str, runner: Runner) -> None:
    """Persist relevant ``Runner`` attributes once the playbook completes.

    Uses the stdout lines already accumulated via ``_append_stdout``
    during execution, falling back to ``runner.stdout`` only when no
    incremental lines were captured.
    """
    with _job_store_lock:
        existing = _job_store.get(job_id)
        incremental_lines = existing["stdout"] if existing else []

    # Fall back to reading runner.stdout when no lines were captured
    # incrementally (e.g. when every event had empty stdout).
    if not incremental_lines:
        stdout_lines = runner.stdout.read().split("\n") if runner.stdout else []
        stdout_lines = [line for line in stdout_lines if line.strip()]
    else:
        stdout_lines = incremental_lines

    with _job_store_lock:
        _job_store[job_id] = {
            "status": runner.status,
            "rc": int(runner.rc) if runner.rc is not None else None,
            "stdout": stdout_lines,
            "stats": runner.stats if runner.stats else {},
            "canceled": getattr(runner, "canceled", False),
            "errored": getattr(runner, "errored", False),
            "timed_out": getattr(runner, "timed_out", False),
        }


def get_job_status(job_id: str) -> dict[str, Any] | None:
    """Return the stored state for *job_id*, or ``None`` if unknown.

    Returns a shallow copy so callers get a consistent snapshot while
    the job store may still be mutated by the runner thread.
    """
    with _job_store_lock:
        entry = _job_store.get(job_id)
        if entry is None:
            return None
        # Shallow copy the dict and snapshot the stdout list so the
        # caller sees a stable view even if new lines are appended.
        snapshot = dict(entry)
        snapshot["stdout"] = list(entry["stdout"])
        return snapshot


class CallbackFailedError(Exception):
    """Exception raised when a callback url can't be reached."""


def playbook_event_handler_factory(
    job_id: str,
    progress: str | None,
    *,
    progress_is_incremental: bool,
) -> Callable[[dict], bool]:
    """Create an event handler for Ansible playbook runs.

    Every event with non-empty stdout is appended to the in-memory job
    store so that ``get_job_status`` returns incremental output while
    the playbook is still running.

    When a *progress* URL is configured the handler also POSTs updates
    to the external system.

    :param str job_id: The job identifier used to look up the store entry.
    :param str progress: The progress URL where the external system expects to receive updates.
    :param bool progress_is_incremental: Whether progress updates are sent incrementally, or contain the whole history
                                         of event data.
    """
    events_stdout: list[str] = []

    def _playbook_event_handler(event: dict) -> bool:
        event_data = event.get("stdout", "").strip()
        if not event_data:
            return False

        event_data_lines = event_data.split("\r\n")
        new_lines = [line for line in event_data_lines if line.strip()]

        # Feed the in-memory job store so poll clients see output
        # while the playbook is still running.
        if new_lines:
            _append_stdout(job_id, new_lines)

        # Optionally forward to an external progress URL.
        if progress:
            if progress_is_incremental:
                emit_body = event_data_lines
            else:
                events_stdout.extend(event_data_lines)
                emit_body = events_stdout

            requests.post(
                str(progress),
                json={"progress": emit_body},
                timeout=settings.REQUEST_TIMEOUT_SEC,
            )

        return True

    return _playbook_event_handler


def playbook_finished_handler_factory(
    callback: str | None, job_id: str
) -> Callable[[Runner], None] | None:
    """Create an event handler for finished Ansible playbook runs.

    Once Ansible runner is finished, it will call the handler method created by this factory before teardown.

    :param str callback: The callback URL that ansible runner should report to.
    :param str job_id: The job ID of this playbook run, used for reporting.
    :return Callable: A handler method that sends one request to the callback URL.
    """

    def _playbook_finished_handler(runner: Runner) -> None:
        playbook_output = runner.stdout.read().split("\n")
        playbook_output = [line for line in playbook_output if line.strip()]

        payload = {
            "status": runner.status,
            "job_id": job_id,
            "output": playbook_output,
            "return_code": int(runner.rc),
        }

        response = requests.post(
            str(callback), json=payload, timeout=settings.REQUEST_TIMEOUT_SEC
        )
        if not (
            status.HTTP_200_OK
            <= response.status_code
            < status.HTTP_300_MULTIPLE_CHOICES
        ):
            msg = f"Callback failed: {response.text}, url: {callback}"
            raise CallbackFailedError(msg)

    if callback:
        return _playbook_finished_handler
    return None


def run_playbook_proc_task(
    job_id: str,
    playbook_path: str,
    extra_vars: dict[str, Any],
    inventory: list[InventoryFile],
    callback: str | None,
    progress: str | None,
    *,
    progress_is_incremental: bool,
    check: bool = False,
    diff: bool = False,
    verbosity: int = 0,
    limit: str | None = None,
    assets: list[InventoryFile] | None = None,
) -> None:
    """Celery task to run a playbook.

    :param str job_id: Identifier of the job being executed.
    :param str playbook_path: Path to the playbook to be executed.
    :param dict[str, Any] extra_vars: Extra variables to pass to the playbook.
    :param list[InventoryFile] inventory: List of inventory file entries written into the ansible-runner
                                          ``private_data_dir/inventory/`` directory.
    :param str callback: Callback URL for status updates.
    :param str progress: URL for sending progress updates.
    :param bool progress_is_incremental: Whether progress updates include all past progress.
    :param bool check: Run Ansible in check mode (dry run).
    :param bool diff: Show diffs for file and template changes.
    :param int verbosity: Ansible verbosity level (0–4), maps to ``-v`` through ``-vvvv``.
    :param str | None limit: Limit execution to a subset of hosts (``--limit``).
    :param list[InventoryFile] | None assets: Optional non-inventory files written to ``private_data_dir/assets/``.
    :return: None
    """
    msg = f"job_id: {job_id}, playbook_path: {playbook_path}, callback: {callback}, check: {check}, diff: {diff}"
    logger.info(msg)

    _register_running_job(job_id)

    cmd_line_args: list[str] = []
    if verbosity:
        cmd_line_args.append(f"-{'v' * min(verbosity, 4)}")
    if check:
        cmd_line_args.append("--check")
    if diff:
        cmd_line_args.append("--diff")

    private_data_dir = tempfile.mkdtemp(prefix="lso-ansible-")
    try:
        inventory_dir = Path(private_data_dir) / "inventory"
        for entry in inventory:
            file_path = inventory_dir / entry["path"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(entry["content"])

        if assets:
            assets_dir = Path(private_data_dir) / "assets"
            for entry in assets:
                file_path = assets_dir / entry["path"]
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(entry["content"])

        runner = run(
            cmdline=" ".join(cmd_line_args) if cmd_line_args else None,
            playbook=playbook_path,
            private_data_dir=private_data_dir,
            limit=limit,
            extravars=extra_vars,
            event_handler=playbook_event_handler_factory(
                job_id, progress, progress_is_incremental=progress_is_incremental
            ),
            finished_callback=playbook_finished_handler_factory(callback, job_id),
        )

        _register_finished_job(job_id, runner)
    finally:
        shutil.rmtree(private_data_dir, ignore_errors=True)


def run_executable_proc_task(
    job_id: str, executable_path: str, args: list[str], callback: str | None
) -> None:
    """Celery task to run an arbitrary executable and notify via callback.

    Executes the executable with the provided arguments and posts back the result if a callback URL is provided.
    """
    from lso.execute import run_executable_sync  # noqa: PLC0415

    msg = f"Executing executable: {executable_path} with args: {args}, callback: {callback}"
    logger.info(msg)
    result = run_executable_sync(executable_path, args)

    if callback:
        payload = ExecutableRunResponse(
            job_id=UUID(job_id),
            result=result,
        ).model_dump(mode="json")

        def _raise_callback_error(message: str, error: Exception | None = None) -> None:
            if error:
                raise CallbackFailedError(message) from error
            raise CallbackFailedError(message)

        try:
            response = requests.post(
                str(callback), json=payload, timeout=settings.REQUEST_TIMEOUT_SEC
            )
            if not (
                status.HTTP_200_OK
                <= response.status_code
                < status.HTTP_300_MULTIPLE_CHOICES
            ):
                msg = f"Callback failed: {response.text}, url: {callback}"
                _raise_callback_error(msg)
        except Exception as e:
            error_msg = f"Callback error: {e}"
            logger.exception(error_msg)
            _raise_callback_error(error_msg, e)

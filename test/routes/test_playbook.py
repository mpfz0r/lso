# Copyright 2023-2024 GÉANT Vereniging.
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
from collections.abc import Callable
from test.utils import temporary_executor
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import responses
from fastapi import status
from fastapi.testclient import TestClient

from lso.config import ExecutorType
from lso.tasks import _job_store, _job_store_lock

TEST_CALLBACK_URL = "https://fqdn.abc.xyz/api/resume"
TEST_PROGRESS_URL = "https://fqdn.abc.xyz/api/progress"

SAMPLE_INVENTORY = {"hosts.yml": "all:\n  hosts:\n    host1_local:\n      foo: bar\n    host2_local:\n      hello: world\n"}


def test_list_playbooks(client: TestClient) -> None:
    """Verify that GET /api/playbook/ returns the available playbooks."""
    rv = client.get("/api/playbook/")
    assert rv.status_code == status.HTTP_200_OK
    response = rv.json()
    assert "playbooks" in response
    assert "placeholder.yaml" in response["playbooks"]


@responses.activate
@pytest.mark.parametrize("callback", [TEST_CALLBACK_URL, None])
@pytest.mark.parametrize("progress", [TEST_PROGRESS_URL, None])
def test_playbook_endpoint_success(
    client: TestClient,
    mocked_ansible_runner_run: Callable,
    callback: str | None,
    progress: str | None,
) -> None:
    params = {
        "playbook_name": "placeholder.yaml",
        "inventory": SAMPLE_INVENTORY,
        "extra_vars": {"dry_run": True, "commit_comment": "I am a robot!"},
    }

    if callback:
        responses.post(url=callback, status=status.HTTP_200_OK)
        params["callback"] = callback
    if progress:
        responses.post(url=progress, status=status.HTTP_200_OK)
        params["progress"] = progress

    with patch("lso.tasks.run", new=mocked_ansible_runner_run):
        rv = client.post("/api/playbook/", json=params)
        assert rv.status_code == status.HTTP_201_CREATED, rv.text
        response = rv.json()

    assert isinstance(response, dict)
    assert isinstance(response["job_id"], str)


@responses.activate
@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize("diff", [True, False])
def test_playbook_endpoint_check_and_diff(
    client: TestClient,
    mocked_ansible_runner_run: Callable,
    check: bool,
    diff: bool,
) -> None:
    responses.post(url=TEST_CALLBACK_URL, status=status.HTTP_200_OK)

    params = {
        "playbook_name": "placeholder.yaml",
        "callback": TEST_CALLBACK_URL,
        "inventory": {"hosts.yml": "all:\n  hosts:\n    host1_local:\n      foo: bar\n"},
        "extra_vars": {},
        "check": check,
        "diff": diff,
    }

    with patch("lso.tasks.run", new=mocked_ansible_runner_run):
        rv = client.post("/api/playbook/", json=params)
        assert rv.status_code == status.HTTP_201_CREATED, rv.text
        response = rv.json()

    assert isinstance(response, dict)
    assert isinstance(response["job_id"], str)


@responses.activate
def test_playbook_endpoint_multi_file_inventory(
    client: TestClient, mocked_ansible_runner_run: Callable
) -> None:
    """Verify that an inventory with multiple files is accepted."""
    responses.post(url=TEST_CALLBACK_URL, status=status.HTTP_200_OK)

    params = {
        "playbook_name": "placeholder.yaml",
        "callback": TEST_CALLBACK_URL,
        "inventory": {
            "hosts.yml": "all:\n  hosts:\n    host1.local:\n    host2.local:\n    host3.local:\n",
            "group_vars/all.yml": "some_var: some_value\n",
        },
    }

    with patch("lso.tasks.run", new=mocked_ansible_runner_run):
        rv = client.post("/api/playbook/", json=params)
        assert rv.status_code == status.HTTP_201_CREATED
        response = rv.json()

    assert isinstance(response, dict)
    assert isinstance(response["job_id"], str)


def test_playbook_endpoint_path_traversal_rejected(client: TestClient) -> None:
    """Inventory paths containing '..' must be rejected."""
    params = {
        "playbook_name": "placeholder.yaml",
        "inventory": {"../../../etc/passwd": "malicious content"},
    }

    rv = client.post("/api/playbook/", json=params)
    assert rv.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_playbook_endpoint_absolute_path_rejected(client: TestClient) -> None:
    """Inventory paths must be relative, not absolute."""
    params = {
        "playbook_name": "placeholder.yaml",
        "inventory": {"/etc/hosts": "content"},
    }

    rv = client.post("/api/playbook/", json=params)
    assert rv.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_playbook_endpoint_inventory_value_must_be_string(client: TestClient) -> None:
    """Inventory values must be strings (YAML content), not dicts or other types."""
    params = {
        "playbook_name": "placeholder.yaml",
        "inventory": {"hosts.yml": {"nested": "dict"}},
    }

    rv = client.post("/api/playbook/", json=params)
    assert rv.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@responses.activate
def test_run_playbook_threadpool_execution(
    client: TestClient, mocked_ansible_runner_run: Callable
) -> None:
    """Test that the playbook runs with ThreadPoolExecutor when ExecutorType is THREADPOOL."""
    with temporary_executor(ExecutorType.THREADPOOL):
        responses.post(url=TEST_CALLBACK_URL, status=status.HTTP_200_OK)

        params = {
            "playbook_name": "placeholder.yaml",
            "extra_vars": {"dry_run": True},
            "inventory": {"hosts.yml": "all:\n  hosts:\n    host1.local:\n      foo: bar\n"},
            "callback": TEST_CALLBACK_URL,
        }

        with (
            patch("lso.tasks.run_playbook_proc_task", new=mocked_ansible_runner_run),
            patch("lso.playbook.get_thread_pool") as mock_get_thread_pool,
        ):
            mock_executor = MagicMock()
            mock_get_thread_pool.return_value = mock_executor
            rv = client.post("/api/playbook/", json=params)

            assert rv.status_code == status.HTTP_201_CREATED
            response = rv.json()

        assert isinstance(response, dict)
        assert isinstance(response["job_id"], str)
        mock_executor.submit.assert_called_once()


def test_get_job_status_not_found(client: TestClient) -> None:
    """GET /api/playbook/{job_id} returns 404 for an unknown job."""
    unknown_id = str(uuid4())
    rv = client.get(f"/api/playbook/{unknown_id}")
    assert rv.status_code == status.HTTP_404_NOT_FOUND


def test_get_job_status_running(client: TestClient) -> None:
    """GET /api/playbook/{job_id} returns running status for an in-progress job."""
    job_id = str(uuid4())
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "running",
            "rc": None,
            "stdout": [],
            "stats": {},
        }

    try:
        rv = client.get(f"/api/playbook/{job_id}")
        assert rv.status_code == status.HTTP_200_OK
        data = rv.json()
        assert data["job_id"] == job_id
        assert data["status"] == "running"
        assert data["rc"] is None
    finally:
        with _job_store_lock:
            _job_store.pop(job_id, None)


def test_get_job_status_successful(client: TestClient) -> None:
    """GET /api/playbook/{job_id} returns full Runner-like payload for a completed job."""
    job_id = str(uuid4())
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "successful",
            "rc": 0,
            "stdout": ["PLAY [all] ***", "TASK [debug] ***", "ok: [host1]"],
            "stats": {"ok": {"host1": 1}},
            "canceled": False,
            "errored": False,
            "timed_out": False,
        }

    try:
        rv = client.get(f"/api/playbook/{job_id}")
        assert rv.status_code == status.HTTP_200_OK
        data = rv.json()
        assert data["job_id"] == job_id
        assert data["status"] == "successful"
        assert data["rc"] == 0
        assert data["stdout"] == ["PLAY [all] ***", "TASK [debug] ***", "ok: [host1]"]
        assert data["stats"] == {"ok": {"host1": 1}}
        assert data["canceled"] is False
        assert data["errored"] is False
        assert data["timed_out"] is False
    finally:
        with _job_store_lock:
            _job_store.pop(job_id, None)


def test_get_job_status_failed(client: TestClient) -> None:
    """GET /api/playbook/{job_id} returns correct data for a failed job."""
    job_id = str(uuid4())
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "failed",
            "rc": 2,
            "stdout": ["ERROR"],
            "stats": {},
            "canceled": False,
            "errored": True,
            "timed_out": False,
        }

    try:
        rv = client.get(f"/api/playbook/{job_id}")
        assert rv.status_code == status.HTTP_200_OK
        data = rv.json()
        assert data["status"] == "failed"
        assert data["rc"] == 2
        assert data["errored"] is True
    finally:
        with _job_store_lock:
            _job_store.pop(job_id, None)

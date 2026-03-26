# Copyright 2023-2025 GÉANT Vereniging.
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

"""The API endpoint from which Ansible playbooks can be executed."""

import logging
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import AfterValidator, BaseModel, HttpUrl

from lso.config import settings
from lso.playbook import get_playbook_path, run_playbook
from lso.schema import InventoryFile
from lso.tasks import get_job_status

router = APIRouter()

logger = logging.getLogger(__name__)


def _validate_file_paths(files: list[InventoryFile], label: str) -> list[InventoryFile]:
    """Validate that all file paths in *files* are safe relative paths.

    :param files: List of file entries with ``path`` and ``content`` fields.
    :param label: Human-readable label used in error messages (e.g. ``"Inventory"``, ``"Asset"``).
    :return: The validated list if all paths are safe.
    :raises HTTPException: If any path is unsafe.
    """
    for entry in files:
        key = entry["path"]
        if not key or not key.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label} contains an empty file path.",
            )
        if "\x00" in key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label} path contains null byte: {key!r}",
            )
        parts = PurePosixPath(key).parts
        if parts[0] == "/":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label} path must be relative, got: {key!r}",
            )
        if ".." in parts:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label} path must not contain '..': {key!r}",
            )
    return files


def _validate_inventory_paths(inventory: list[InventoryFile]) -> list[InventoryFile]:
    return _validate_file_paths(inventory, "Inventory")


def _validate_asset_paths(assets: list[InventoryFile]) -> list[InventoryFile]:
    return _validate_file_paths(assets, "Asset")


def _playbook_path_validator(playbook_name: Path) -> Path:
    playbook_path = get_playbook_path(playbook_name)
    if not Path.exists(playbook_path):
        msg = f"Filename '{playbook_path}' does not exist."
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)

    return playbook_path


PlaybookInventory = Annotated[
    list[InventoryFile], AfterValidator(_validate_inventory_paths)
]
PlaybookAssets = Annotated[list[InventoryFile], AfterValidator(_validate_asset_paths)]
PlaybookName = Annotated[Path, AfterValidator(_playbook_path_validator)]


class PlaybookList(BaseModel):
    """Response model containing the list of available playbooks."""

    playbooks: list[str]


class PlaybookRunResponse(BaseModel):
    """PlaybookRunResponse domain model schema."""

    job_id: UUID


class PlaybookJobStatus(BaseModel):
    """Response model reflecting the state of a playbook job.

    The fields mirror key attributes of :class:`ansible_runner.Runner`.
    """

    job_id: UUID
    status: str
    rc: int | None = None
    stdout: list[str] = []
    stats: dict[str, Any] = {}
    canceled: bool = False
    errored: bool = False
    timed_out: bool = False


class PlaybookRunParams(BaseModel):
    """Parameters for executing an Ansible playbook."""

    #: The filename of a playbook that's executed. It should be present inside the directory defined in the
    #: configuration option ``ANSIBLE_PLAYBOOKS_ROOT_DIR``.
    playbook_name: PlaybookName
    #: The address where LSO should call back to upon completion.
    callback: HttpUrl | None = None
    #: Optionally, the address where LSO should send progress updates as the playbook executes.
    progress: HttpUrl | None = None
    #: Optionally, whether progress updates should be incremental or not.
    progress_is_incremental: bool = True
    #: The inventory to run the playbook against, as a list of file entries. Each entry has a ``path`` (relative path
    #: within the inventory directory, e.g. ``"hosts.yml"``, ``"group_vars/routers.yml"``) and ``content`` (the YAML
    #: file contents as a string).
    inventory: PlaybookInventory
    #: Extra variables that should get passed to the playbook. This includes any required configuration objects
    #: from the workflow orchestrator, commit comments, whether this execution should be a dry run, a trouble ticket
    #: number, etc. Which extra vars are required solely depends on what inputs the playbook requires.
    extra_vars: dict[str, Any] = {}
    #: When enabled, Ansible runs in check mode (``--check``), simulating changes without applying them.
    check: bool = False
    #: When enabled, Ansible shows file diffs (``--diff``) for any template or file changes.
    diff: bool = False
    #: Ansible verbosity level (0–4). Maps to ``-v`` through ``-vvvv``. Default ``0`` means no extra verbosity.
    verbosity: int = 0
    #: Limit execution to a subset of hosts. Accepts the same patterns as Ansible's ``--limit`` flag:
    #: host names, group names, comma-separated lists, or wildcard patterns.
    limit: str | None = None
    #: Optional list of non-inventory files needed by the playbook. Each entry has a ``path`` (relative path within
    #: the assets directory) and ``content`` (file contents as a string). Files are written to
    #: ``private_data_dir/assets/`` before execution.
    assets: PlaybookAssets = []


@router.get(
    "/",
    response_model=PlaybookList,
    summary="List available playbooks",
)
def list_playbooks() -> PlaybookList:
    """Return the names of all Ansible playbooks available on this LSO instance.

    Playbooks are discovered by scanning the directory configured in ``ANSIBLE_PLAYBOOKS_ROOT_DIR``
    for files with a ``.yaml`` or ``.yml`` extension.

    :return PlaybookList: A list of relative playbook filenames.
    """
    root = Path(settings.ANSIBLE_PLAYBOOKS_ROOT_DIR)
    if not root.is_dir():
        return PlaybookList(playbooks=[])
    playbooks = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml"}
    )
    return PlaybookList(playbooks=playbooks)


@router.post(
    "/",
    response_model=PlaybookRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run an Ansible playbook",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Playbook file not found"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Invalid inventory or request body"
        },
    },
)
def run_playbook_endpoint(params: PlaybookRunParams) -> PlaybookRunResponse:
    """Launch an Ansible playbook to modify or deploy a subscription instance.

    The response will contain either a job ID, or error information.

    :param PlaybookRunParams params: Parameters for executing a playbook.
    :return JSONResponse: Response from the Ansible runner, including a run ID.
    """
    job_id = run_playbook(
        playbook_path=params.playbook_name,
        extra_vars=params.extra_vars,
        inventory=params.inventory,
        callback=params.callback,
        progress=params.progress,
        progress_is_incremental=params.progress_is_incremental,
        check=params.check,
        diff=params.diff,
        verbosity=params.verbosity,
        limit=params.limit,
        assets=params.assets,
    )

    return PlaybookRunResponse(job_id=job_id)


@router.get(
    "/{job_id}",
    response_model=PlaybookJobStatus,
    summary="Get playbook job status",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Job not found"},
    },
)
def get_playbook_job_status(job_id: UUID) -> PlaybookJobStatus:
    """Fetch the progress or final result of a playbook run.

    Returns a payload that mirrors the key attributes of the
    :class:`ansible_runner.Runner` object associated with this job.

    :param UUID job_id: Identifier of the playbook job.
    :return PlaybookJobStatus: Current status of the job.
    """
    job = get_job_status(str(job_id))
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found."
        )

    return PlaybookJobStatus(job_id=job_id, **job)

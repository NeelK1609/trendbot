"""Where a paper (or live) account's state lives: a single JSON document, saved only if nobody changed it since it was read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class ConflictError(RuntimeError):
    """The state changed since it was loaded (another run got there first). Nothing was saved."""


class Store(Protocol):
    def load(self) -> tuple[dict | None, str | None]:
        """Returns (state, version); (None, None) if there is no account yet."""

    def save(self, state: dict, version: str | None) -> str:
        """Saves if the stored version still equals `version` (None: only if no state exists yet)."""


class LocalStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[dict | None, str | None]:
        if not self.path.exists():
            return None, None
        return json.loads(self.path.read_text()), str(self.path.stat().st_mtime_ns)

    def save(self, state: dict, version: str | None) -> str:
        current = str(self.path.stat().st_mtime_ns) if self.path.exists() else None
        if current != version:
            raise ConflictError(f"{self.path} changed since it was read")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self.path)
        return str(self.path.stat().st_mtime_ns)


class BlobStore:
    """Azure Blob Storage with Entra ID auth (managed identity in Azure, `az login` locally)."""

    def __init__(self, account: str, container: str, blob: str = "account.json"):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential())
        self.client = service.get_blob_client(container, blob)

    def load(self) -> tuple[dict | None, str | None]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            download = self.client.download_blob()
        except ResourceNotFoundError:
            return None, None
        return json.loads(download.readall()), download.properties.etag

    def save(self, state: dict, version: str | None) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

        body = json.dumps(state, indent=1).encode()
        try:
            if version is None:
                result = self.client.upload_blob(body, overwrite=False)
            else:
                result = self.client.upload_blob(body, overwrite=True, etag=version, match_condition=MatchConditions.IfNotModified)
        except (ResourceExistsError, ResourceModifiedError) as e:
            raise ConflictError("The account changed since it was read") from e
        return result["etag"]

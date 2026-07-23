"""Runtime-owned file descriptor input for immutable ingestion flows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lfx.custom import Component
from lfx.io import DictInput, Output, StrInput
from lfx.schema import Data

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DeploymentFileInputComponent(Component):
    display_name = "Deployment File Input"
    description = "Receives a file descriptor supplied by the on-premise runtime."
    icon = "file-input"
    name = "DeploymentFileInput"

    inputs = [
        StrInput(name="file_path", display_name="File Path", required=True),
        StrInput(name="document_id", display_name="Document ID", required=True),
        StrInput(name="checksum", display_name="SHA-256", required=True),
        StrInput(name="mime_type", display_name="MIME Type", required=True),
        DictInput(name="metadata", display_name="Metadata", advanced=True),
    ]
    outputs = [Output(name="document", display_name="Document", method="build_document")]

    def build_document(self) -> Data:
        path = Path(self.file_path)
        if not path.is_absolute():
            msg = "Deployment file path must be absolute"
            raise ValueError(msg)
        checksum = self.checksum.removeprefix("sha256:").lower()
        if not _SHA256_RE.fullmatch(checksum):
            msg = "Deployment file checksum must be a SHA-256 digest"
            raise ValueError(msg)
        if not self.document_id.strip() or not self.mime_type.strip():
            msg = "Document ID and MIME type are required"
            raise ValueError(msg)
        descriptor: dict[str, Any] = {
            "file_path": str(path),
            "document_id": self.document_id.strip(),
            "checksum": f"sha256:{checksum}",
            "mime_type": self.mime_type.strip().lower(),
            "metadata": self.metadata or {},
        }
        self.status = descriptor
        return Data(data=descriptor)

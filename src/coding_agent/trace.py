"""Local JSONL audit records for model API requests and responses."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class APITraceRecorder:
    """Append inspectable API events to one stable file per workspace."""

    def __init__(self, directory: str | Path, workspace: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session_id = uuid.uuid4().hex
        self.file_path = self.directory / self._project_filename(self.workspace)
        self._lock = threading.Lock()
        self._append(
            {
                "event": "session_start",
                "session_id": self.session_id,
                "workspace": str(self.workspace),
            }
        )

    def record_request(self, payload: dict[str, Any]) -> str:
        request_id = uuid.uuid4().hex
        self._append(
            {
                "event": "api_request",
                "session_id": self.session_id,
                "request_id": request_id,
                "workspace": str(self.workspace),
                "payload": payload,
            }
        )
        return request_id

    def record_response(
        self,
        request_id: str,
        response_body: bytes,
        *,
        attempt: int,
    ) -> None:
        text = response_body.decode("utf-8", errors="replace")
        try:
            response: Any = json.loads(text)
        except json.JSONDecodeError:
            response = text
        self._append(
            {
                "event": "api_response",
                "session_id": self.session_id,
                "request_id": request_id,
                "attempt": attempt,
                "response": response,
            }
        )

    def record_error(
        self,
        request_id: str,
        *,
        attempt: int,
        error_type: str,
        message: str,
        retryable: bool,
        response_body: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "event": "api_error",
            "session_id": self.session_id,
            "request_id": request_id,
            "attempt": attempt,
            "error_type": error_type,
            "message": message,
            "retryable": retryable,
        }
        if response_body is not None:
            event["response_body"] = response_body
        self._append(event)

    def _append(self, event: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.file_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized + "\n")

    @staticmethod
    def _project_filename(workspace: Path) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace.name).strip("-._")
        if not name:
            name = "project"
        identity = str(workspace).casefold().encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:12]
        return f"{name}-{digest}.jsonl"

"""Thread-safe persistence for UI projects and conversations."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_INVALID_PROJECT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class StateError(ValueError):
    """Raised when persisted UI state or a requested entity is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UIStateStore:
    """Persist small UI metadata atomically; project source stays in its workspace."""

    def __init__(self, state_file: str | Path, projects_root: str | Path) -> None:
        self.state_file = Path(state_file).expanduser().resolve()
        self.projects_root = Path(projects_root).expanduser().resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state = self._load()

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            projects = [self._project_summary(project) for project in self._state["projects"]]
        return sorted(projects, key=lambda item: item["updated_at"], reverse=True)

    def create_project(self, name: str) -> dict[str, Any]:
        clean_name = self._validate_project_name(name)
        with self._lock:
            destination = (self.projects_root / clean_name).resolve()
            try:
                destination.relative_to(self.projects_root)
            except ValueError as exc:
                raise StateError("项目目录必须位于 UI 项目根目录中。") from exc
            if any(
                Path(project["path"]).resolve() == destination
                for project in self._state["projects"]
            ):
                raise StateError("同名项目已经存在。")
            if destination.exists() and any(destination.iterdir()):
                raise StateError("目标目录已存在且不是空目录。")
            destination.mkdir(parents=True, exist_ok=True)
            now = utc_now()
            conversation = self._new_conversation("新对话", now)
            project = {
                "id": uuid.uuid4().hex,
                "name": clean_name,
                "path": str(destination),
                "created_at": now,
                "updated_at": now,
                "conversations": [conversation],
            }
            self._state["projects"].append(project)
            self._save()
            return deepcopy(project)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._find_project(project_id))

    def create_conversation(self, project_id: str, title: str = "新对话") -> dict[str, Any]:
        clean_title = self._clean_title(title)
        with self._lock:
            project = self._find_project(project_id)
            now = utc_now()
            conversation = self._new_conversation(clean_title, now)
            project["conversations"].append(conversation)
            project["updated_at"] = now
            self._save()
            return deepcopy(conversation)

    def get_conversation(self, conversation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._lock:
            project, conversation = self._find_conversation(conversation_id)
            return deepcopy(project), deepcopy(conversation)

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        events: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "error"}:
            raise StateError(f"不支持的消息角色: {role}")
        if not isinstance(content, str) or not content.strip():
            raise StateError("消息内容不能为空。")
        with self._lock:
            project, conversation = self._find_conversation(conversation_id)
            now = utc_now()
            message = {
                "id": uuid.uuid4().hex,
                "role": role,
                "content": content.strip(),
                "created_at": now,
            }
            if events:
                message["events"] = deepcopy(events)
            if metadata:
                message["metadata"] = deepcopy(metadata)
            conversation["messages"].append(message)
            if role == "user" and conversation["title"] == "新对话":
                conversation["title"] = self._clean_title(content, maximum=36)
            conversation["updated_at"] = now
            project["updated_at"] = now
            self._save()
            return deepcopy(message)

    def model_history(self, conversation_id: str) -> list[dict[str, str]]:
        """Return only completed user/assistant pairs; errors never enter model context."""

        with self._lock:
            _project, conversation = self._find_conversation(conversation_id)
            messages = deepcopy(conversation["messages"])
        history: list[dict[str, str]] = []
        pending_user: dict[str, str] | None = None
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                pending_user = {"role": "user", "content": content}
            elif role == "assistant" and pending_user is not None and isinstance(content, str):
                history.extend([pending_user, {"role": "assistant", "content": content}])
                pending_user = None
            elif role == "error":
                pending_user = None
        return history

    def _load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"version": 1, "projects": []}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(f"无法读取 UI 状态文件: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise StateError("UI 状态文件版本无效。")
        if not isinstance(data.get("projects"), list):
            raise StateError("UI 状态文件缺少 projects 数组。")
        return data

    def _save(self) -> None:
        serialized = json.dumps(self._state, ensure_ascii=False, indent=2)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=self.state_file.parent,
                prefix=f".{self.state_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(serialized)
                temporary_name = stream.name
            os.replace(temporary_name, self.state_file)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _find_project(self, project_id: str) -> dict[str, Any]:
        for project in self._state["projects"]:
            if project.get("id") == project_id:
                return project
        raise StateError("项目不存在。")

    def _find_conversation(
        self, conversation_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        for project in self._state["projects"]:
            for conversation in project.get("conversations", []):
                if conversation.get("id") == conversation_id:
                    return project, conversation
        raise StateError("对话不存在。")

    @staticmethod
    def _new_conversation(title: str, now: str) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

    @staticmethod
    def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": project["id"],
            "name": project["name"],
            "path": project["path"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "conversations": [
                {
                    "id": conversation["id"],
                    "title": conversation["title"],
                    "created_at": conversation["created_at"],
                    "updated_at": conversation["updated_at"],
                    "message_count": len(conversation["messages"]),
                }
                for conversation in project.get("conversations", [])
            ],
        }

    @staticmethod
    def _validate_project_name(name: str) -> str:
        if not isinstance(name, str):
            raise StateError("项目名必须是字符串。")
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 80:
            raise StateError("项目名长度必须在 1 到 80 个字符之间。")
        if clean_name in {".", ".."} or _INVALID_PROJECT_CHARS.search(clean_name):
            raise StateError("项目名包含不能用于目录的字符。")
        if clean_name.endswith((".", " ")):
            raise StateError("项目名不能以点或空格结尾。")
        if clean_name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise StateError("项目名是系统保留名称。")
        return clean_name

    @staticmethod
    def _clean_title(title: str, *, maximum: int = 80) -> str:
        if not isinstance(title, str):
            return "新对话"
        collapsed = " ".join(title.strip().split())
        if not collapsed:
            return "新对话"
        return collapsed[:maximum]

"""Background execution bridge between Tkinter and the core coding agent."""

from __future__ import annotations

import threading
import traceback
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Protocol

from coding_agent.agent import CodingAgent, RunResult
from coding_agent.client import ChatCompletionsClient
from coding_agent.config import Settings
from coding_agent.errors import AgentError
from coding_agent.tools import WorkspaceTools
from coding_agent.trace import APITraceRecorder
from coding_agent.ui.state import StateError, UIStateStore, utc_now


class RunnableAgent(Protocol):
    def run(
        self,
        task: str,
        *,
        history: list[dict[str, str]] | None = None,
    ) -> RunResult: ...


EventHandler = Callable[[str, dict[str, Any]], None]
AgentFactory = Callable[[str, EventHandler], RunnableAgent]


class JobManager:
    """Run Agent tasks off the Tk event loop and expose immutable snapshots."""

    def __init__(
        self,
        store: UIStateStore,
        trace_directory: str | Path,
        *,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self._store = store
        self._trace_directory = Path(trace_directory).expanduser().resolve()
        self._agent_factory = agent_factory or self._build_agent
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_conversations: set[str] = set()
        self._lock = threading.RLock()

    def start(self, conversation_id: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise StateError("消息内容不能为空。")
        if len(content) > 100_000:
            raise StateError("单条消息不能超过 100,000 个字符。")
        project, _conversation = self._store.get_conversation(conversation_id)
        history = self._store.model_history(conversation_id)
        with self._lock:
            if conversation_id in self._active_conversations:
                raise StateError("该对话已有任务正在运行。")
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "conversation_id": conversation_id,
                "status": "running",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "events": [],
                "result": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._active_conversations.add(conversation_id)
            try:
                self._store.append_message(conversation_id, "user", content)
            except Exception:
                self._jobs.pop(job_id, None)
                self._active_conversations.discard(conversation_id)
                raise
            thread = threading.Thread(
                target=self._run,
                args=(
                    job_id,
                    project["path"],
                    conversation_id,
                    content.strip(),
                    history,
                ),
                name=f"coding-agent-job-{job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise StateError("任务不存在或程序已经重启。")
            return deepcopy(job)

    def _run(
        self,
        job_id: str,
        workspace: str,
        conversation_id: str,
        content: str,
        history: list[dict[str, str]],
    ) -> None:
        def event_handler(kind: str, payload: dict[str, Any]) -> None:
            event = {"kind": kind, "payload": deepcopy(payload), "created_at": utc_now()}
            with self._lock:
                self._jobs[job_id]["events"].append(event)
                self._jobs[job_id]["updated_at"] = utc_now()

        try:
            agent = self._agent_factory(workspace, event_handler)
            result = agent.run(content, history=history)
            with self._lock:
                events = deepcopy(self._jobs[job_id]["events"])
            self._store.append_message(
                conversation_id,
                "assistant",
                result.final_text,
                events=events,
                metadata={
                    "iterations": result.iterations,
                    "stop_reason": result.stop_reason,
                },
            )
            with self._lock:
                self._jobs[job_id]["status"] = "completed"
                self._jobs[job_id]["result"] = {
                    "final_text": result.final_text,
                    "iterations": result.iterations,
                    "stop_reason": result.stop_reason,
                }
        except AgentError as exc:
            self._fail_job(job_id, conversation_id, str(exc))
        except Exception as exc:  # Defensive boundary: a worker must not close the UI.
            traceback.print_exc()
            self._fail_job(job_id, conversation_id, f"桌面 UI 后台任务异常: {exc}")
        finally:
            with self._lock:
                self._jobs[job_id]["updated_at"] = utc_now()
                self._active_conversations.discard(conversation_id)

    def _build_agent(self, workspace: str, event_handler: EventHandler) -> CodingAgent:
        settings = Settings.from_sources()
        tools = WorkspaceTools(
            workspace,
            output_limit=settings.max_tool_output_chars,
            command_timeout=settings.command_timeout_seconds,
        )
        trace_recorder = APITraceRecorder(self._trace_directory, tools.root)
        return CodingAgent(
            ChatCompletionsClient(settings, trace_recorder),
            tools,
            max_iterations=settings.max_iterations,
            max_context_chars=settings.max_context_chars,
            repeated_call_limit=settings.repeated_call_limit,
            event_handler=event_handler,
        )

    def _fail_job(self, job_id: str, conversation_id: str, message: str) -> None:
        with self._lock:
            events = deepcopy(self._jobs[job_id]["events"])
        try:
            self._store.append_message(
                conversation_id,
                "error",
                message,
                events=events,
            )
        finally:
            with self._lock:
                self._jobs[job_id]["status"] = "failed"
                self._jobs[job_id]["error"] = message

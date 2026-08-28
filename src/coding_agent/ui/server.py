"""Framework-free local HTTP server for project and conversation management."""

from __future__ import annotations

import argparse
import json
import os
import threading
import traceback
import uuid
import webbrowser
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from coding_agent.agent import CodingAgent
from coding_agent.client import ChatCompletionsClient
from coding_agent.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_ITERATIONS,
    Settings,
)
from coding_agent.errors import AgentError
from coding_agent.tools import WorkspaceTools
from coding_agent.ui.state import StateError, UIStateStore, utc_now


STATIC_ROOT = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 1_000_000


class JobManager:
    """Run one agent job per conversation in a background thread."""

    def __init__(self, store: UIStateStore) -> None:
        self._store = store
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
                args=(job_id, project["path"], conversation_id, content.strip(), history),
                name=f"coding-agent-job-{job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise StateError("任务不存在或服务已经重启。")
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
            settings = Settings.from_sources()
            tools = WorkspaceTools(
                workspace,
                output_limit=settings.max_tool_output_chars,
                command_timeout=settings.command_timeout_seconds,
            )
            agent = CodingAgent(
                ChatCompletionsClient(settings),
                tools,
                max_iterations=settings.max_iterations,
                max_context_chars=settings.max_context_chars,
                repeated_call_limit=settings.repeated_call_limit,
                event_handler=event_handler,
            )
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
        except Exception as exc:  # Defensive boundary: never crash the HTTP server thread.
            traceback.print_exc()
            self._fail_job(job_id, conversation_id, f"UI 后台任务异常: {exc}")
        finally:
            with self._lock:
                self._jobs[job_id]["updated_at"] = utc_now()
                self._active_conversations.discard(conversation_id)

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


class AgentUIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: UIStateStore,
        jobs: JobManager,
    ) -> None:
        super().__init__(server_address, AgentUIHandler)
        self.store = store
        self.jobs = jobs


class AgentUIHandler(BaseHTTPRequestHandler):
    server: AgentUIHTTPServer
    server_version = "CodingAgentUI/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            path = unquote(urlparse(self.path).path)
            if path == "/api/bootstrap":
                self._json(
                    HTTPStatus.OK,
                    {"runtime": self._runtime_status(), "projects": self.server.store.list_projects()},
                )
                return
            if path.startswith("/api/conversations/"):
                conversation_id = self._segments(path, 3)[2]
                project, conversation = self.server.store.get_conversation(conversation_id)
                self._json(
                    HTTPStatus.OK,
                    {
                        "project": {
                            "id": project["id"],
                            "name": project["name"],
                            "path": project["path"],
                        },
                        "conversation": conversation,
                    },
                )
                return
            if path.startswith("/api/jobs/"):
                job_id = self._segments(path, 3)[2]
                self._json(HTTPStatus.OK, self.server.jobs.get(job_id))
                return
            self._serve_static(path)
        except StateError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务器内部错误: {exc}"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            path = unquote(urlparse(self.path).path)
            payload = self._read_json()
            if path == "/api/projects":
                project = self.server.store.create_project(payload.get("name"))
                self._json(HTTPStatus.CREATED, project)
                return
            segments = self._segments(path)
            if len(segments) == 4 and segments[:2] == ["api", "projects"] and segments[3] == "conversations":
                conversation = self.server.store.create_conversation(
                    segments[2], payload.get("title", "新对话")
                )
                self._json(HTTPStatus.CREATED, conversation)
                return
            if len(segments) == 4 and segments[:2] == ["api", "conversations"] and segments[3] == "messages":
                job = self.server.jobs.start(segments[2], payload.get("content"))
                self._json(HTTPStatus.ACCEPTED, job)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})
        except StateError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"请求格式无效: {exc}"})
        except Exception as exc:
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务器内部错误: {exc}"})

    def _serve_static(self, path: str) -> None:
        mapping = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        target = mapping.get(path)
        if target is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "页面不存在。"})
            return
        body = (STATIC_ROOT / target[0]).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", target[1])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("缺少 Content-Length。")
        length = int(raw_length)
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求体超过大小限制。")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return value

    @staticmethod
    def _segments(path: str, expected: int | None = None) -> list[str]:
        segments = [segment for segment in path.strip("/").split("/") if segment]
        if expected is not None and len(segments) != expected:
            raise ValueError("URL 路径格式无效。")
        return segments

    @staticmethod
    def _runtime_status() -> dict[str, Any]:
        agent_key = os.getenv("CODING_AGENT_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")
        key_source = (
            "CODING_AGENT_API_KEY"
            if agent_key
            else "OPENAI_API_KEY" if openai_key else None
        )
        agent_model = os.getenv("CODING_AGENT_MODEL")
        openai_model = os.getenv("OPENAI_MODEL")
        model_source = (
            "CODING_AGENT_MODEL"
            if agent_model
            else "OPENAI_MODEL" if openai_model else None
        )
        model = agent_model or openai_model
        base_url = (
            os.getenv("CODING_AGENT_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        )
        expected_names = {
            "CODING_AGENT_API_KEY",
            "CODING_AGENT_MODEL",
            "CODING_AGENT_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "OPENAI_BASE_URL",
        }
        misnamed_variables = sorted(
            name
            for name in os.environ
            if "\\_" in name and name.replace("\\_", "_") in expected_names
        )
        return {
            "api_ready": bool(key_source and model),
            "key_configured": bool(key_source),
            "key_source": key_source,
            "model": model,
            "model_source": model_source,
            "base_url": base_url,
            "misnamed_variables": misnamed_variables,
            "max_iterations": DEFAULT_MAX_ITERATIONS,
            "max_context_chars": DEFAULT_MAX_CONTEXT_CHARS,
        }

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[UI] {self.address_string()} - {format % args}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动本地编程智能体 Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument(
        "--projects-root",
        default="workspaces",
        help="UI 创建项目的父目录（默认：./workspaces）",
    )
    parser.add_argument(
        "--state-file",
        default=".coding-agent/ui-state.json",
        help="项目与对话历史文件（默认已被 Git 忽略）",
    )
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1 到 65535 之间。")
    store = UIStateStore(args.state_file, args.projects_root)
    server = AgentUIHTTPServer((args.host, args.port), store, JobManager(store))
    url = f"http://{args.host}:{server.server_address[1]}"
    print(f"Coding Agent UI 已启动：{url}")
    print(f"项目目录：{store.projects_root}")
    print("按 Ctrl+C 停止。")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 UI...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

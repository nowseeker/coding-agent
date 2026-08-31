from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.agent import RunResult
from coding_agent.errors import AgentError
from coding_agent.ui.app import build_parser
from coding_agent.ui.jobs import JobManager
from coding_agent.ui.runtime import runtime_status
from coding_agent.ui.state import StateError, UIStateStore


class UIStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = UIStateStore(self.root / "state.json", self.root / "projects")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_project_and_conversation_are_persisted(self) -> None:
        project = self.store.create_project("demo")
        conversation = project["conversations"][0]
        self.store.append_message(conversation["id"], "user", "创建程序")
        self.store.append_message(conversation["id"], "assistant", "已经完成")

        reloaded = UIStateStore(self.root / "state.json", self.root / "projects")
        projects = reloaded.list_projects()
        _project, saved_conversation = reloaded.get_conversation(conversation["id"])

        self.assertEqual(projects[0]["name"], "demo")
        self.assertTrue(Path(project["path"]).is_dir())
        self.assertEqual(saved_conversation["title"], "创建程序")
        self.assertEqual(
            reloaded.model_history(conversation["id"]),
            [
                {"role": "user", "content": "创建程序"},
                {"role": "assistant", "content": "已经完成"},
            ],
        )

    def test_failed_turn_is_not_sent_back_to_model(self) -> None:
        project = self.store.create_project("demo")
        conversation_id = project["conversations"][0]["id"]
        self.store.append_message(conversation_id, "user", "失败任务")
        self.store.append_message(conversation_id, "error", "网络错误")

        self.assertEqual(self.store.model_history(conversation_id), [])

    def test_unsafe_project_names_are_rejected(self) -> None:
        for name in ("../outside", "bad/name", "CON", "name."):
            with self.subTest(name=name), self.assertRaises(StateError):
                self.store.create_project(name)


class RuntimeStatusTests(unittest.TestCase):
    def test_runtime_status_reports_sources_without_secret_value(self) -> None:
        environment = {
            "CODING_AGENT_API_KEY": "must-not-appear",
            "CODING_AGENT_MODEL": "deepseek-v4-flash",
            "CODING_AGENT_BASE_URL": "https://api.deepseek.com",
        }
        with patch.dict(os.environ, environment, clear=True):
            status = runtime_status()

        self.assertTrue(status["api_ready"])
        self.assertEqual(status["key_source"], "CODING_AGENT_API_KEY")
        self.assertEqual(status["model"], "deepseek-v4-flash")
        self.assertNotIn("must-not-appear", json.dumps(status))

    def test_runtime_status_detects_backslashes_in_variable_names(self) -> None:
        environment = {
            "CODING\\_AGENT\\_API\\_KEY": "wrong",
            "CODING_AGENT_MODEL": "deepseek-v4-flash",
        }
        with patch.dict(os.environ, environment, clear=True):
            status = runtime_status()

        self.assertFalse(status["api_ready"])
        self.assertEqual(
            status["misnamed_variables"],
            ["CODING\\_AGENT\\_API\\_KEY"],
        )


class JobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = UIStateStore(self.root / "state.json", self.root / "projects")
        project = self.store.create_project("demo")
        self.workspace = project["path"]
        self.conversation_id = project["conversations"][0]["id"]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _wait(manager: JobManager, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = manager.get(job_id)
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        raise AssertionError("UI background job did not finish")

    def test_successful_job_persists_answer_and_events(self) -> None:
        observed: dict[str, object] = {}

        class FakeAgent:
            def __init__(self, event_handler):
                self.event_handler = event_handler

            def run(self, task, *, history=None):
                observed["task"] = task
                observed["history"] = history
                self.event_handler("iteration", {"number": 1})
                self.event_handler("tool_start", {"name": "list_files"})
                return RunResult("任务完成", 1, "verified_completed")

        def factory(workspace, event_handler):
            observed["workspace"] = workspace
            return FakeAgent(event_handler)

        manager = JobManager(
            self.store,
            self.root / "traces",
            agent_factory=factory,
        )

        started = manager.start(self.conversation_id, "检查项目")
        completed = self._wait(manager, started["id"])
        _project, conversation = self.store.get_conversation(self.conversation_id)

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(observed["workspace"], self.workspace)
        self.assertEqual(observed["history"], [])
        self.assertEqual(
            [message["role"] for message in conversation["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual(
            conversation["messages"][-1]["metadata"]["stop_reason"],
            "verified_completed",
        )
        self.assertEqual(len(conversation["messages"][-1]["events"]), 2)

    def test_failed_job_is_visible_but_not_added_to_model_history(self) -> None:
        class FailingAgent:
            def run(self, task, *, history=None):
                raise AgentError("模型连接失败")

        manager = JobManager(
            self.store,
            self.root / "traces",
            agent_factory=lambda _workspace, _handler: FailingAgent(),
        )

        started = manager.start(self.conversation_id, "执行任务")
        failed = self._wait(manager, started["id"])

        self.assertEqual(failed["status"], "failed")
        self.assertIn("模型连接失败", failed["error"])
        self.assertEqual(self.store.model_history(self.conversation_id), [])


class DesktopUIParserTests(unittest.TestCase):
    def test_parser_uses_local_state_and_has_no_web_server_options(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        destinations = {action.dest for action in parser._actions}

        self.assertEqual(args.projects_root, "workspaces")
        self.assertEqual(args.state_file, ".coding-agent/desktop-ui-state.json")
        self.assertNotIn("host", destinations)
        self.assertNotIn("port", destinations)
        self.assertNotIn("open", destinations)


if __name__ == "__main__":
    unittest.main()

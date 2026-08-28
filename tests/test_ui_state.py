from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from coding_agent.ui.server import AgentUIHTTPServer, AgentUIHandler, JobManager
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


class UIHTTPTests(unittest.TestCase):
    def test_runtime_status_reports_exact_environment_sources(self) -> None:
        environment = {
            "CODING_AGENT_API_KEY": "secret",
            "CODING_AGENT_MODEL": "deepseek-v4-flash",
            "CODING_AGENT_BASE_URL": "https://api.deepseek.com",
        }
        with patch.dict(os.environ, environment, clear=True):
            status = AgentUIHandler._runtime_status()

        self.assertTrue(status["api_ready"])
        self.assertEqual(status["key_source"], "CODING_AGENT_API_KEY")
        self.assertEqual(status["model_source"], "CODING_AGENT_MODEL")
        self.assertNotIn("secret", json.dumps(status))

    def test_runtime_status_detects_backslashes_in_variable_names(self) -> None:
        environment = {
            "CODING\\_AGENT\\_API\\_KEY": "wrong",
            "CODING_AGENT_MODEL": "deepseek-v4-flash",
        }
        with patch.dict(os.environ, environment, clear=True):
            status = AgentUIHandler._runtime_status()

        self.assertFalse(status["api_ready"])
        self.assertEqual(
            status["misnamed_variables"],
            ["CODING\\_AGENT\\_API\\_KEY"],
        )

    def test_bootstrap_create_project_and_static_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UIStateStore(root / "state.json", root / "projects")
            server = AgentUIHTTPServer(("127.0.0.1", 0), store, JobManager(store))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base_url}/") as response:
                    page = response.read().decode("utf-8")
                request = Request(
                    f"{base_url}/api/projects",
                    data=json.dumps({"name": "web-demo"}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as response:
                    project = json.loads(response.read().decode("utf-8"))
                with urlopen(f"{base_url}/api/bootstrap") as response:
                    bootstrap = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertIn("Coding Agent", page)
            self.assertEqual(project["name"], "web-demo")
            self.assertEqual(bootstrap["projects"][0]["id"], project["id"])


if __name__ == "__main__":
    unittest.main()

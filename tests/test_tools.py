from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.errors import ToolError
from coding_agent.tools import ToolSpec, WorkspaceTools


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tools = WorkspaceTools(self.root, output_limit=2_000, command_timeout=10)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_write_read_replace_search_and_list(self) -> None:
        write_result = json.loads(
            self.tools.execute(
                "write_file",
                {"path": "src/example.py", "content": "first\nsecond\n"},
            )
        )
        self.assertTrue(write_result["ok"])

        read_result = json.loads(
            self.tools.execute("read_file", {"path": "src/example.py", "start_line": 2})
        )
        self.assertEqual(read_result["output"], "2: second")

        replace_result = json.loads(
            self.tools.execute(
                "replace_in_file",
                {"path": "src/example.py", "old": "second", "new": "changed"},
            )
        )
        self.assertTrue(replace_result["ok"])

        search_result = json.loads(
            self.tools.execute(
                "search_text", {"query": "CHANGED", "path": "src", "file_glob": "*.py"}
            )
        )
        self.assertIn("src/example.py:2", search_result["output"])

        list_result = json.loads(self.tools.execute("list_files", {"path": "."}))
        self.assertIn("src/example.py", list_result["output"])

    def test_path_traversal_is_rejected(self) -> None:
        result = json.loads(self.tools.execute("read_file", {"path": "../outside.txt"}))
        self.assertFalse(result["ok"])
        self.assertIn("工作区之外", result["error"])

    def test_absolute_path_inside_workspace_is_allowed(self) -> None:
        target = self.root / "inside.txt"
        target.write_text("ok", encoding="utf-8")
        result = json.loads(self.tools.execute("read_file", {"path": str(target)}))
        self.assertTrue(result["ok"])

    def test_git_metadata_and_env_files_are_rejected(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("data", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=x", encoding="utf-8")
        git_result = json.loads(self.tools.execute("read_file", {"path": ".git/config"}))
        env_result = json.loads(self.tools.execute("read_file", {"path": ".env"}))
        self.assertFalse(git_result["ok"])
        self.assertFalse(env_result["ok"])

        env_variant = json.loads(
            self.tools.execute(
                "write_file", {"path": ".env.production", "content": "SECRET=x"}
            )
        )
        self.assertFalse(env_variant["ok"])

    def test_replace_requires_exact_occurrence_count(self) -> None:
        (self.root / "sample.txt").write_text("x x", encoding="utf-8")
        result = json.loads(
            self.tools.execute(
                "replace_in_file",
                {"path": "sample.txt", "old": "x", "new": "y", "expected_occurrences": 1},
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / "sample.txt").read_text(encoding="utf-8"), "x x")

    def test_dangerous_command_is_rejected(self) -> None:
        result = json.loads(self.tools.execute("run_command", {"command": "git reset --hard"}))
        self.assertFalse(result["ok"])
        self.assertIn("安全策略", result["error"])

    def test_command_environment_removes_credentials(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-leak"}, clear=False):
            result = json.loads(
                self.tools.execute(
                    "run_command",
                    {
                        "command": (
                            'python -c "import os; '
                            "print(os.getenv('OPENAI_API_KEY', 'missing'))\""
                        )
                    },
                )
            )
        self.assertTrue(result["ok"])
        self.assertIn("missing", result["output"])
        self.assertNotIn("must-not-leak", result["output"])

    def test_output_is_truncated(self) -> None:
        tools = WorkspaceTools(self.root, output_limit=20)
        (self.root / "long.txt").write_text("a" * 100, encoding="utf-8")
        result = json.loads(tools.execute("read_file", {"path": "long.txt"}))
        self.assertIn("截断", result["output"])

    def test_invalid_arguments_do_not_raise(self) -> None:
        result = json.loads(self.tools.execute("list_files", {"unexpected": True}))
        self.assertFalse(result["ok"])
        self.assertIn("参数无效", result["error"])
        self.assertEqual(result["error_code"], "invalid_tool_arguments")
        self.assertEqual(result["details"][0]["code"], "additional_property")

    def test_tool_schema_required_fields_and_defaults_come_from_handler(self) -> None:
        schemas = {
            item["function"]["name"]: item["function"]
            for item in self.tools.schemas()
        }

        list_parameters = schemas["list_files"]["parameters"]
        self.assertEqual(list_parameters["required"], [])
        self.assertEqual(list_parameters["properties"]["path"]["default"], ".")
        self.assertIs(list_parameters["properties"]["recursive"]["default"], True)
        self.assertEqual(list_parameters["properties"]["max_results"]["default"], 200)

        read_parameters = schemas["read_file"]["parameters"]
        self.assertEqual(read_parameters["required"], ["path"])
        self.assertEqual(read_parameters["properties"]["start_line"]["default"], 1)
        self.assertNotIn("default", read_parameters["properties"]["end_line"])

    def test_strict_validation_does_not_coerce_json_types(self) -> None:
        cases = [
            ({"recursive": "false"}, "recursive", "boolean", "string"),
            ({"max_results": True}, "max_results", "integer", "boolean"),
        ]

        for arguments, field, expected, received in cases:
            with self.subTest(arguments=arguments):
                result = json.loads(self.tools.execute("list_files", arguments))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "invalid_tool_arguments")
                issue = result["details"][0]
                self.assertEqual(issue["field"], field)
                self.assertEqual(issue["code"], "type_mismatch")
                self.assertEqual(issue["expected"], expected)
                self.assertEqual(issue["received"], received)

    def test_strict_validation_reports_all_argument_issues(self) -> None:
        result = json.loads(
            self.tools.execute(
                "read_file",
                {
                    "start_line": 0,
                    "end_line": "20",
                    "unexpected": True,
                },
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_tool_arguments")
        issues = {(issue["field"], issue["code"]) for issue in result["details"]}
        self.assertEqual(
            issues,
            {
                ("path", "required"),
                ("unexpected", "additional_property"),
                ("start_line", "minimum"),
                ("end_line", "type_mismatch"),
            },
        )

    def test_schema_results_cannot_mutate_registry(self) -> None:
        first = self.tools.schemas()
        first[0]["function"]["parameters"]["properties"]["path"]["type"] = "integer"

        second = self.tools.schemas()

        self.assertEqual(
            second[0]["function"]["parameters"]["properties"]["path"]["type"],
            "string",
        )

    def test_tool_spec_rejects_schema_handler_drift(self) -> None:
        def handler(path: str) -> str:
            return path

        spec = ToolSpec(
            name="broken_tool",
            description="A deliberately invalid test tool.",
            parameters={"other": {"type": "string"}},
            handler=handler,
        )

        with self.assertRaisesRegex(ToolError, "参数不一致"):
            spec.validate_contract()

    def test_direct_invalid_workspace_raises(self) -> None:
        with self.assertRaises(ToolError):
            WorkspaceTools(self.root / "missing")


if __name__ == "__main__":
    unittest.main()

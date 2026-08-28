from __future__ import annotations

import unittest
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from coding_agent.agent import RunResult
from coding_agent.cli import _run_interactive_session, build_parser
from coding_agent.errors import AgentError


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def run(self, task, *, history=None):
        self.calls.append((task, deepcopy(history or [])))
        return RunResult(f"完成：{task}", 1, "completed")


class FailingOnceAgent(FakeAgent):
    def run(self, task, *, history=None):
        self.calls.append((task, deepcopy(history or [])))
        if task == "失败任务":
            raise AgentError("模拟失败")
        return RunResult(f"完成：{task}", 1, "completed")


class CLITests(unittest.TestCase):
    def test_interactive_session_carries_completed_history(self) -> None:
        agent = FakeAgent()
        answers = iter(["第一个任务", "继续修改", "/exit"])

        with patch("sys.stdout", new=StringIO()):
            exit_code = _run_interactive_session(
                agent,
                input_fn=lambda _prompt: next(answers),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(agent.calls[0], ("第一个任务", []))
        self.assertEqual(
            agent.calls[1],
            (
                "继续修改",
                [
                    {"role": "user", "content": "第一个任务"},
                    {"role": "assistant", "content": "完成：第一个任务"},
                ],
            ),
        )

    def test_failed_turn_does_not_enter_follow_up_history(self) -> None:
        agent = FailingOnceAgent()
        answers = iter(["失败任务", "重试任务", "/exit"])

        with patch("sys.stdout", new=StringIO()), patch("sys.stderr", new=StringIO()):
            _run_interactive_session(agent, input_fn=lambda _prompt: next(answers))

        self.assertEqual(agent.calls[1], ("重试任务", []))

    def test_new_command_clears_history_without_ending_session(self) -> None:
        agent = FakeAgent()
        answers = iter(["第一个任务", "/new", "新任务", "/exit"])

        with patch("sys.stdout", new=StringIO()):
            _run_interactive_session(agent, input_fn=lambda _prompt: next(answers))

        self.assertEqual(agent.calls[1], ("新任务", []))

    def test_interactive_flag_is_available(self) -> None:
        args = build_parser().parse_args(["--interactive", "初始任务"])
        self.assertTrue(args.interactive)
        self.assertEqual(args.task, ["初始任务"])


if __name__ == "__main__":
    unittest.main()

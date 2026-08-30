"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Sequence

from coding_agent.agent import CodingAgent
from coding_agent.client import ChatCompletionsClient
from coding_agent.config import Settings
from coding_agent.errors import AgentError
from coding_agent.tools import WorkspaceTools
from coding_agent.trace import APITraceRecorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="使用 OpenAI 兼容模型原生 tool calling 的本地编程智能体",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="要完成的编程任务；在终端中省略时进入多轮交互会话",
    )
    parser.add_argument("--workspace", default=".", help="智能体可访问的工作区（默认：当前目录）")
    parser.add_argument("--model", help="模型名；默认读取 CODING_AGENT_MODEL/OPENAI_MODEL")
    parser.add_argument("--base-url", help="兼容网关的 v1 base URL")
    parser.add_argument("--api-timeout", type=int, help="单次 API 请求超时秒数")
    parser.add_argument("--command-timeout", type=int, help="本地命令默认超时秒数")
    parser.add_argument("--max-iterations", type=int, help="最大模型循环次数")
    parser.add_argument("--context-chars", type=int, help="发送给模型的近似上下文字符预算")
    parser.add_argument("--tool-output-chars", type=int, help="单次工具输出字符上限")
    parser.add_argument(
        "--trace-dir",
        help="API 输入输出审计目录（默认：仓库 .coding-agent/api-traces）",
    )
    parser.add_argument("--quiet", action="store_true", help="只打印模型最终回答")
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="完成命令行给出的首个任务后继续多轮对话",
    )
    return parser


def _read_noninteractive_task(parts: list[str]) -> str:
    if parts:
        return " ".join(parts).strip()
    return sys.stdin.read().strip()


def _default_trace_directory() -> Path:
    return Path(__file__).resolve().parents[2] / ".coding-agent" / "api-traces"


def _event_printer(quiet: bool):
    def handle(kind: str, payload: dict[str, object]) -> None:
        if quiet:
            return
        if kind == "iteration":
            print(f"\n[第 {payload['number']} 轮]", flush=True)
        elif kind == "assistant":
            print(str(payload["text"]), flush=True)
        elif kind == "tool_start":
            print(f"  -> {payload['name']}", flush=True)
        elif kind == "tool_end":
            state = "完成" if payload.get("ok") else "失败"
            print(f"  <- {payload['name']}：{state}", flush=True)

    return handle


def _print_result(result, quiet: bool) -> None:
    if quiet:
        print(result.final_text)
    else:
        print(f"\n[完成，共 {result.iterations} 轮]\n{result.final_text}")


def _run_interactive_session(
    agent: CodingAgent,
    *,
    initial_task: str = "",
    quiet: bool = False,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Keep completed user/assistant pairs in memory for follow-up tasks."""

    history: list[dict[str, str]] = []
    task = initial_task.strip()
    if not quiet:
        print("已进入多轮会话。输入 /new 清空对话上下文，输入 /exit 退出。")

    while True:
        if not task:
            try:
                task = input_fn("\n你> ").strip()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print("\n已退出多轮会话。")
                return 130

        command = task.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            if not quiet:
                print("已退出多轮会话。")
            return 0
        if command == "/new":
            history.clear()
            task = ""
            if not quiet:
                print("已清空对话上下文；工作区文件不会回滚。")
            continue
        if command == "/help":
            print("命令：/new 清空对话上下文；/exit 退出。")
            task = ""
            continue

        try:
            result = agent.run(task, history=history)
        except KeyboardInterrupt:
            print("\n当前任务已中断，可以继续输入下一条任务。", file=sys.stderr)
        except AgentError as exc:
            print(f"错误：{exc}", file=sys.stderr)
        else:
            _print_result(result, quiet)
            history.extend(
                [
                    {"role": "user", "content": task},
                    {"role": "assistant", "content": result.final_text},
                ]
            )
        task = ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_sources(
            model=args.model,
            base_url=args.base_url,
            api_timeout_seconds=args.api_timeout,
            max_iterations=args.max_iterations,
            max_context_chars=args.context_chars,
            max_tool_output_chars=args.tool_output_chars,
            command_timeout_seconds=args.command_timeout,
        )
        tools = WorkspaceTools(
            Path(args.workspace),
            output_limit=settings.max_tool_output_chars,
            command_timeout=settings.command_timeout_seconds,
        )
        trace_directory = (
            Path(args.trace_dir).expanduser()
            if args.trace_dir
            else _default_trace_directory()
        )
        trace_recorder = APITraceRecorder(trace_directory, tools.root)
        if not args.quiet:
            print(f"API 输入输出记录：{trace_recorder.file_path}")
        agent = CodingAgent(
            ChatCompletionsClient(settings, trace_recorder),
            tools,
            max_iterations=settings.max_iterations,
            max_context_chars=settings.max_context_chars,
            repeated_call_limit=settings.repeated_call_limit,
            event_handler=_event_printer(args.quiet),
        )
        initial_task = " ".join(args.task).strip()
        interactive = args.interactive or (not initial_task and sys.stdin.isatty())
        if interactive:
            return _run_interactive_session(
                agent,
                initial_task=initial_task,
                quiet=args.quiet,
            )

        task = _read_noninteractive_task(args.task)
        if not task:
            raise AgentError("任务描述不能为空。")
        result = agent.run(task)
        _print_result(result, args.quiet)
        return 0
    except KeyboardInterrupt:
        print("\n已由用户中断。", file=sys.stderr)
        return 130
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

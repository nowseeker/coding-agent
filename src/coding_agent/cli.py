"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from coding_agent.agent import CodingAgent
from coding_agent.client import ChatCompletionsClient
from coding_agent.config import Settings
from coding_agent.errors import AgentError
from coding_agent.tools import WorkspaceTools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="使用 OpenAI 兼容模型原生 tool calling 的本地编程智能体",
    )
    parser.add_argument("task", nargs="*", help="要完成的编程任务；省略时从标准输入读取")
    parser.add_argument("--workspace", default=".", help="智能体可访问的工作区（默认：当前目录）")
    parser.add_argument("--model", help="模型名；默认读取 CODING_AGENT_MODEL/OPENAI_MODEL")
    parser.add_argument("--base-url", help="兼容网关的 v1 base URL")
    parser.add_argument("--api-timeout", type=int, help="单次 API 请求超时秒数")
    parser.add_argument("--command-timeout", type=int, help="本地命令默认超时秒数")
    parser.add_argument("--max-iterations", type=int, help="最大模型循环次数")
    parser.add_argument("--context-chars", type=int, help="发送给模型的近似上下文字符预算")
    parser.add_argument("--tool-output-chars", type=int, help="单次工具输出字符上限")
    parser.add_argument("--quiet", action="store_true", help="只打印模型最终回答")
    return parser


def _read_task(parts: list[str]) -> str:
    if parts:
        return " ".join(parts).strip()
    if sys.stdin.isatty():
        return input("请输入编程任务：").strip()
    return sys.stdin.read().strip()


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        task = _read_task(args.task)
        if not task:
            raise AgentError("任务描述不能为空。")
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
        agent = CodingAgent(
            ChatCompletionsClient(settings),
            tools,
            max_iterations=settings.max_iterations,
            max_context_chars=settings.max_context_chars,
            repeated_call_limit=settings.repeated_call_limit,
            event_handler=_event_printer(args.quiet),
        )
        result = agent.run(task)
        if args.quiet:
            print(result.final_text)
        else:
            print(f"\n[完成，共 {result.iterations} 轮]\n{result.final_text}")
        return 0
    except KeyboardInterrupt:
        print("\n已由用户中断。", file=sys.stderr)
        return 130
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

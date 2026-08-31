"""Tkinter desktop interface for the local coding agent."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Sequence

from coding_agent.ui.jobs import JobManager
from coding_agent.ui.runtime import runtime_status
from coding_agent.ui.state import StateError, UIStateStore


BACKGROUND = "#0d1117"
SIDEBAR = "#161b22"
PANEL = "#11161d"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#2f81f7"
SUCCESS = "#3fb950"
ERROR = "#f85149"
USER = "#a5d6ff"
ASSISTANT = "#d2a8ff"


class DesktopApp:
    """Own widgets only; Agent decisions remain in the core package."""

    def __init__(self, root: tk.Tk, store: UIStateStore, jobs: JobManager) -> None:
        self.root = root
        self.store = store
        self.jobs = jobs
        self.project_ids: list[str] = []
        self.current_project_id: str | None = None
        self.current_conversation_id: str | None = None
        self.active_job_id: str | None = None
        self.seen_event_count = 0
        self._build_window()
        self._build_layout()
        self.refresh_projects()
        self._refresh_runtime_status()
        self.root.after(250, self._poll_job)

    def _build_window(self) -> None:
        self.root.title("Coding Agent")
        self.root.geometry("1180x760")
        self.root.minsize(900, 620)
        self.root.configure(bg=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Sidebar.TFrame", background=SIDEBAR)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure(
            "TButton",
            background=SIDEBAR,
            foreground=TEXT,
            bordercolor=BORDER,
            focusthickness=1,
            focuscolor=ACCENT,
            padding=(10, 7),
        )
        style.map("TButton", background=[("active", "#21262d")])
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
        )
        style.map("Accent.TButton", background=[("active", "#1f6feb")])
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
        style.configure("Muted.TLabel", background=BACKGROUND, foreground=MUTED)
        style.configure("Sidebar.TLabel", background=SIDEBAR, foreground=TEXT)

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True)

        sidebar = ttk.Frame(outer, style="Sidebar.TFrame", width=260)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)
        main = ttk.Frame(outer, style="Panel.TFrame")
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        ttk.Label(
            sidebar,
            text="Coding Agent",
            style="Sidebar.TLabel",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor=tk.W, padx=18, pady=(20, 4))
        ttk.Label(
            sidebar,
            text="本地项目",
            style="Sidebar.TLabel",
            foreground=MUTED,
        ).pack(anchor=tk.W, padx=18, pady=(0, 12))

        button_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        button_row.pack(fill=tk.X, padx=12, pady=(0, 10))
        ttk.Button(button_row, text="新建项目", command=self._create_project).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        self.new_chat_button = ttk.Button(
            button_row,
            text="新对话",
            command=self._new_conversation,
        )
        self.new_chat_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        list_frame = tk.Frame(sidebar, bg=SIDEBAR)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12)
        self.project_list = tk.Listbox(
            list_frame,
            bg=SIDEBAR,
            fg=TEXT,
            selectbackground="#1f6feb",
            selectforeground="#ffffff",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            activestyle="none",
            font=("Segoe UI", 10),
        )
        project_scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.project_list.yview
        )
        self.project_list.configure(yscrollcommand=project_scroll.set)
        self.project_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        project_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.project_list.bind("<<ListboxSelect>>", self._on_project_selected)

        self.sidebar_status = tk.Label(
            sidebar,
            text="",
            bg=SIDEBAR,
            fg=MUTED,
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=225,
            font=("Segoe UI", 9),
        )
        self.sidebar_status.pack(fill=tk.X, padx=16, pady=16)

        header = tk.Frame(main, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        header.grid(row=0, column=0, sticky="ew")
        self.project_title = tk.Label(
            header,
            text="请选择或创建项目",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI", 13, "bold"),
            anchor=tk.W,
        )
        self.project_title.pack(fill=tk.X, padx=20, pady=(14, 2))
        self.project_path = tk.Label(
            header,
            text="",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor=tk.W,
        )
        self.project_path.pack(fill=tk.X, padx=20, pady=(0, 12))

        chat_frame = tk.Frame(main, bg=BACKGROUND)
        chat_frame.grid(row=1, column=0, sticky="nsew", padx=18, pady=(16, 8))
        chat_frame.columnconfigure(0, weight=1)
        chat_frame.rowconfigure(0, weight=1)
        self.chat = tk.Text(
            chat_frame,
            height=1,
            bg=BACKGROUND,
            fg=TEXT,
            insertbackground=TEXT,
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.WORD,
            padx=12,
            pady=12,
            font=("Segoe UI", 10),
            spacing1=2,
            spacing3=8,
            state=tk.DISABLED,
        )
        chat_scroll = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self.chat.yview)
        self.chat.configure(yscrollcommand=chat_scroll.set)
        self.chat.grid(row=0, column=0, sticky="nsew")
        chat_scroll.grid(row=0, column=1, sticky="ns")
        self.chat.tag_configure("user_label", foreground=USER, font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure(
            "assistant_label", foreground=ASSISTANT, font=("Segoe UI", 10, "bold")
        )
        self.chat.tag_configure("error_label", foreground=ERROR, font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("body", foreground=TEXT, lmargin1=4, lmargin2=4)
        self.chat.tag_configure("progress", foreground=MUTED, lmargin1=18, lmargin2=18)
        self.chat.tag_configure("success", foreground=SUCCESS, lmargin1=18, lmargin2=18)

        composer = tk.Frame(main, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        composer.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 18))
        composer.columnconfigure(0, weight=1)
        tk.Label(
            composer,
            text="输入任务",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor=tk.W,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 0))
        self.input = tk.Text(
            composer,
            height=4,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=12,
            pady=10,
            font=("Segoe UI", 10),
        )
        self.input.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        self.input.bind("<Control-Return>", self._send_shortcut)
        self.send_button = ttk.Button(
            composer,
            text="发送\nCtrl+Enter",
            style="Accent.TButton",
            command=self._send_task,
        )
        self.send_button.grid(row=1, column=1, sticky="se", padx=10, pady=10)

    def refresh_projects(self, select_id: str | None = None) -> None:
        projects = self.store.list_projects()
        self.project_ids = [project["id"] for project in projects]
        self.project_list.delete(0, tk.END)
        for project in projects:
            self.project_list.insert(tk.END, project["name"])
        target = select_id or self.current_project_id
        if target in self.project_ids:
            index = self.project_ids.index(target)
            self.project_list.selection_set(index)
            self.project_list.activate(index)
            self._select_project(target)
        elif projects:
            self.project_list.selection_set(0)
            self._select_project(projects[0]["id"])
        else:
            self.current_project_id = None
            self.current_conversation_id = None
            self._render_empty()

    def _create_project(self) -> None:
        name = simpledialog.askstring(
            "新建项目",
            f"项目将创建在：\n{self.store.projects_root}\n\n请输入项目名：",
            parent=self.root,
        )
        if name is None:
            return
        try:
            project = self.store.create_project(name)
        except (StateError, OSError) as exc:
            messagebox.showerror("无法创建项目", str(exc), parent=self.root)
            return
        self.refresh_projects(project["id"])
        self.input.focus_set()

    def _new_conversation(self) -> None:
        if not self.current_project_id:
            messagebox.showinfo("尚未选择项目", "请先创建或选择项目。", parent=self.root)
            return
        if self.active_job_id:
            messagebox.showinfo("任务运行中", "请等待当前任务结束。", parent=self.root)
            return
        conversation = self.store.create_conversation(self.current_project_id)
        self.current_conversation_id = conversation["id"]
        self._render_conversation(conversation)
        self.input.focus_set()

    def _on_project_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.project_list.curselection()
        if not selection or self.active_job_id:
            return
        self._select_project(self.project_ids[selection[0]])

    def _select_project(self, project_id: str) -> None:
        try:
            project = self.store.get_project(project_id)
        except StateError as exc:
            messagebox.showerror("项目状态错误", str(exc), parent=self.root)
            return
        self.current_project_id = project_id
        conversations = project.get("conversations", [])
        if not conversations:
            conversation = self.store.create_conversation(project_id)
        else:
            conversation = conversations[-1]
        self.current_conversation_id = conversation["id"]
        self.project_title.configure(text=project["name"])
        self.project_path.configure(text=project["path"])
        self._render_conversation(conversation)

    def _render_empty(self) -> None:
        self.project_title.configure(text="请选择或创建项目")
        self.project_path.configure(text="")
        self._replace_chat(
            [("assistant_label", "Coding Agent\n"), ("body", "从左侧新建一个本地项目开始。\n")]
        )

    def _render_conversation(self, conversation: dict[str, Any]) -> None:
        chunks: list[tuple[str, str]] = []
        for message in conversation.get("messages", []):
            role = message.get("role")
            if role == "user":
                chunks.extend(
                    [("user_label", "你\n"), ("body", f"{message.get('content', '')}\n\n")]
                )
            elif role == "assistant":
                chunks.extend(
                    [
                        ("assistant_label", "Agent\n"),
                        ("body", f"{message.get('content', '')}\n\n"),
                    ]
                )
            elif role == "error":
                chunks.extend(
                    [("error_label", "错误\n"), ("body", f"{message.get('content', '')}\n\n")]
                )
        if not chunks:
            chunks = [
                ("assistant_label", "Coding Agent\n"),
                ("body", "描述一个编程任务。Agent 会在该项目目录中读取、修改并验证代码。\n"),
            ]
        self._replace_chat(chunks)

    def _replace_chat(self, chunks: list[tuple[str, str]]) -> None:
        self.chat.configure(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        for tag, text in chunks:
            self.chat.insert(tk.END, text, tag)
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def _append_chat(self, text: str, tag: str = "progress") -> None:
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, text, tag)
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def _send_shortcut(self, _event: tk.Event[Any]) -> str:
        self._send_task()
        return "break"

    def _send_task(self) -> None:
        if not self.current_conversation_id:
            messagebox.showinfo("尚未选择项目", "请先创建或选择项目。", parent=self.root)
            return
        if self.active_job_id:
            return
        status = runtime_status()
        if not status["api_ready"]:
            messagebox.showerror(
                "模型配置不完整",
                "请在启动该窗口的终端中设置 API Key 和模型名，然后重新启动 UI。",
                parent=self.root,
            )
            return
        content = self.input.get("1.0", tk.END).strip()
        if not content:
            return
        try:
            job = self.jobs.start(self.current_conversation_id, content)
        except (StateError, OSError) as exc:
            messagebox.showerror("无法启动任务", str(exc), parent=self.root)
            return
        self.input.delete("1.0", tk.END)
        _project, conversation = self.store.get_conversation(self.current_conversation_id)
        self._render_conversation(conversation)
        self._append_chat("Agent 正在分析任务...\n", "progress")
        self.active_job_id = job["id"]
        self.seen_event_count = 0
        self._set_running(True)

    def _poll_job(self) -> None:
        if self.active_job_id:
            try:
                job = self.jobs.get(self.active_job_id)
                events = job.get("events", [])
                for event in events[self.seen_event_count :]:
                    self._render_event(event)
                self.seen_event_count = len(events)
                if job["status"] in {"completed", "failed"}:
                    conversation_id = job["conversation_id"]
                    self.active_job_id = None
                    self.seen_event_count = 0
                    self._set_running(False)
                    if conversation_id == self.current_conversation_id:
                        _project, conversation = self.store.get_conversation(conversation_id)
                        self._render_conversation(conversation)
                    self.refresh_projects(self.current_project_id)
            except (StateError, tk.TclError) as exc:
                self.active_job_id = None
                self._set_running(False)
                if self.root.winfo_exists():
                    messagebox.showerror("任务状态错误", str(exc), parent=self.root)
        if self.root.winfo_exists():
            self.root.after(250, self._poll_job)

    def _render_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        payload = event.get("payload") or {}
        if kind == "iteration":
            self._append_chat(f"第 {payload.get('number', '?')} 轮\n")
        elif kind == "tool_start":
            self._append_chat(f"  → {payload.get('name', 'unknown')}\n")
        elif kind == "tool_end":
            marker = "✓" if payload.get("ok") else "✗"
            tag = "success" if payload.get("ok") else "progress"
            self._append_chat(f"  {marker} {payload.get('name', 'unknown')}\n", tag)
        elif kind == "assistant" and payload.get("text"):
            self._append_chat(f"  {payload['text']}\n")
        elif kind == "completion_rejected":
            self._append_chat("  完成请求被证据门拒绝，模型将继续修正。\n")

    def _set_running(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        self.send_button.configure(state=state)
        self.new_chat_button.configure(state=state)
        self.project_list.configure(state=state)
        self.input.configure(state=state)
        self.project_title.configure(
            text=(self.project_title.cget("text") + " · 运行中")
            if running and not str(self.project_title.cget("text")).endswith("· 运行中")
            else str(self.project_title.cget("text")).replace(" · 运行中", "")
        )
        if not running:
            self.input.focus_set()

    def _refresh_runtime_status(self) -> None:
        status = runtime_status()
        if status["api_ready"]:
            text = (
                f"模型：{status['model']}\n"
                f"API Key：已设置\n"
                f"接口：{status['base_url']}"
            )
            color = SUCCESS
        else:
            missing = []
            if not status["key_configured"]:
                missing.append("API Key")
            if not status["model"]:
                missing.append("模型名")
            text = "配置缺少：" + "、".join(missing)
            color = ERROR
        if status["misnamed_variables"]:
            text += "\n发现变量名中含有错误的 \\_"
            color = ERROR
        self.sidebar_status.configure(text=text, fg=color)

    def _on_close(self) -> None:
        if self.active_job_id and not messagebox.askyesno(
            "任务仍在运行",
            "关闭窗口会停止显示任务结果。仍要关闭吗？",
            parent=self.root,
        ):
            return
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动本地编程智能体桌面 UI")
    parser.add_argument(
        "--projects-root",
        default="workspaces",
        help="UI 创建项目的父目录（默认：./workspaces）",
    )
    parser.add_argument(
        "--state-file",
        default=".coding-agent/desktop-ui-state.json",
        help="本地项目与对话状态文件（默认已被 Git 忽略）",
    )
    parser.add_argument(
        "--trace-dir",
        default=".coding-agent/api-traces",
        help="本地 API 输入输出审计目录（默认已被 Git 忽略）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法启动桌面窗口: {exc}", file=sys.stderr)
        return 1
    try:
        store = UIStateStore(args.state_file, args.projects_root)
        jobs = JobManager(store, args.trace_dir)
        DesktopApp(root, store, jobs)
        root.mainloop()
    except (StateError, OSError) as exc:
        messagebox.showerror("无法启动 Coding Agent", str(exc), parent=root)
        root.destroy()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

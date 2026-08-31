编程智能体（Coding Agent）

Git 仓库
https://github.com/nowseeker/coding-agent

运行环境
Python 3.10+，无第三方运行时依赖。模型接口需兼容 /chat/completions 请求格式，并能够返回结构化的 tool_calls。所有文件操作和命令执行均由本项目在本地完成。

运行方法
1. 在仓库根目录打开 PowerShell。启动前在环境变量中配置 CODING_AGENT_API_KEY、CODING_AGENT_MODEL；使用兼容网关时可另设 CODING_AGENT_BASE_URL。
2. 桌面窗口：python ui.py
   左侧创建或选择项目，中间输入任务并查看对话和工具执行过程。
3. CLI 多轮模式：python agent.py --workspace <工作区路径>
   /new 清空对话上下文，/exit 退出。
4. 测试：python -m unittest discover -s tests -t . -v

特色功能
项目未使用任何 Agent 框架或 Agent SDK。桌面界面由 Python 标准库 Tkinter 独立实现，直接调用本项目核心，不是在现成 Agent 产品上封装界面。项目自行实现模型请求解析、ReAct 工具循环、上下文管理、错误处理和终止条件。模型通过原生 tool calling 提出工具调用，由项目解析、严格校验并在本地执行 ToolSpec 工具，完成文件列表、读取、搜索、写入、精确替换和受限命令执行。文件路径限制在工作区内。

支持严格请求预算、按工具语义压缩历史、Python AST 代码结构与行号分析、失败输出保留、重复调用熔断、API 重试及 finish_task 证据完成门。修改文件后必须运行成功的验证命令，才能提交完成结果。每个项目的 API 请求和响应记录在本地忽略目录，便于审计。

其它说明
API Key 只从环境变量读取，不写入状态文件，也不会传给工具子进程。模型 API 只负责推理；文件操作和命令执行均由本项目在本机完成，不依赖服务端托管的代码执行或文件工具。桌面状态和审计日志保存在 .coding-agent/，可能包含任务或代码。程序不是系统级沙箱，请仅在可控工作区中运行并人工审查改动。

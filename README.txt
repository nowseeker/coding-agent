编程智能体（Coding Agent）

Git 仓库：https://github.com/nowseeker/coding-agent

运行环境：Python 3.10+，无第三方运行时依赖。

1. 设置凭据和模型（PowerShell）：
   $env:CODING_AGENT_API_KEY="你的 API key"
   $env:CODING_AGENT_MODEL="支持 tool calling 的模型名"
   $env:CODING_AGENT_BASE_URL="模型服务商的 OpenAI 兼容 v1 地址"
2. Web UI：python ui.py --open
   默认访问 http://127.0.0.1:8765；左侧创建/选择项目和历史对话，中间提交任务，右侧查看模型轮次与工具执行轨迹。
3. CLI 多轮会话：python agent.py --workspace 工作区路径
   完成一项任务后可继续输入修改要求；/new 清空对话上下文，/exit 退出。命令后直接附加任务则执行一次后退出；加 --interactive 可在首个任务后继续对话。
4. 测试：python -m unittest discover -s tests -t . -v

特色功能：自行实现模型输出解析、跨轮对话历史与确定性上下文压缩、六种本地文件/命令工具、结构化错误反馈、API 重试、命令超时、路径越界和敏感文件保护、工具输出截断、重复调用熔断与最大轮次终止。模型只生成文本和 tool calls，本地程序完成实际文件及命令操作，不使用 Code Interpreter、Files API 或 Agent SDK。

API key 仅从环境变量读取，不进入浏览器、历史文件或子进程。项目默认位于 workspaces/，UI 历史位于 .coding-agent/，均不入库。本工具不是系统级沙箱，请只在可控工作区运行并审查改动。

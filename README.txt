编程智能体（Coding Agent）

Git 仓库：https://github.com/nowseeker/coding-agent

运行环境：Python 3.10+，无第三方运行时依赖。

1. 设置凭据和模型（PowerShell）：
   $env:OPENAI_API_KEY="你的 API key"
   $env:OPENAI_MODEL="支持 tool calling 的模型名"
   # 使用兼容网关时可选：$env:OPENAI_BASE_URL="https://example.com/v1"
2. 多轮运行：python agent.py --workspace .
   出现“你>”后连续输入任务；/new 清空对话上下文，/exit 退出。
3. 单次运行：python agent.py --workspace . "实现一个带测试的待办事项命令行程序"
   追加 --interactive 可在首个任务后继续对话。
4. 查看参数：python agent.py --help
5. 测试：python -m unittest discover -s tests -t . -v

特色功能：使用模型原生 tool calling，自主读取/搜索/修改工作区文件并执行本地命令；支持终端多轮会话、上下文压缩、严格工具参数校验、证据完成门、每项目本地 API 轮次审计、API 重试、命令超时与输出截断、路径越界和危险命令拦截、重复工具调用熔断。对话历史仅在当前进程内保存。API key 仅从环境变量读取，且不会传给子进程。main 分支不包含 Web UI，UI 实验保留在 ui 分支。

可选安装：python -m pip install -e .，之后可用 coding-agent 命令。智能体并非系统级沙箱，请只在可控工作区中运行并在提交前审查改动。

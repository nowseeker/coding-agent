编程智能体（Coding Agent）

Git 仓库：https://github.com/nowseeker/coding-agent

运行环境：Python 3.10+，无第三方运行时依赖。

1. 设置凭据和模型（PowerShell）：
   $env:OPENAI_API_KEY="你的 API key"
   $env:OPENAI_MODEL="支持 tool calling 的模型名"
   # 使用兼容网关时可选：$env:OPENAI_BASE_URL="https://example.com/v1"
2. 桌面运行：python ui.py。左侧创建/选择项目，中间连续对话并显示工具执行过程。
3. CLI 多轮运行：python agent.py --workspace .；/new 清空上下文，/exit 退出。
4. 单次运行：python agent.py --workspace . "实现一个带测试的待办事项命令行程序"
   追加 --interactive 可在首个任务后继续对话。
5. 测试：python -m unittest discover -s tests -t . -v

特色功能：使用模型原生 tool calling，自主读取/搜索/修改文件并执行本地命令；提供无 Web 服务的 Tkinter 桌面窗口和 CLI；支持多轮历史、严格请求预算、工具语义压缩、代码结构与行号分析、严格参数校验、证据完成门、本地 API 轮次审计、API 重试、命令超时、路径越界和危险命令拦截。结构摘要只用于定位，修改前读取精确代码。API key 仅从环境变量读取，不写入状态文件且不传给子进程。

可选安装：python -m pip install -e .，之后可用 coding-agent 命令。智能体并非系统级沙箱，请只在可控工作区中运行并在提交前审查改动。

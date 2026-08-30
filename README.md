# coding-agent

一个不依赖 Agent 框架的本地编程智能体。模型通过 OpenAI 兼容的 Chat Completions
接口返回原生 tool calling；工具的定义、参数校验、文件访问和命令执行均由本项目在本机完成。

## 当前范围

- 支持终端多轮会话，并把已成功完成的用户/助手消息传给后续任务。
- 支持读取、搜索、写入和修改工作区文件，以及执行受限制的本地命令。
- 使用 ToolSpec 单一注册表生成工具 Schema、绑定本地处理函数并严格校验参数，不进行隐式类型转换。
- 支持最大循环次数、重复工具调用熔断、命令超时、输出截断和路径越界拦截。
- 按字符预算保留最近的完整对话对和工具调用块，并对较早内容做确定性压缩。
- API Key 只从环境变量读取，不写入仓库，也不会传给工具子进程。

`main` 分支当前不包含 Web UI；UI 实验代码保留在 `ui` 分支。

## 配置

PowerShell 当前窗口临时配置示例：

```powershell
$env:CODING_AGENT_API_KEY = "你的 API Key"
$env:CODING_AGENT_MODEL = "支持原生 tool calling 的模型名"
$env:CODING_AGENT_BASE_URL = "https://兼容服务地址/v1"
```

也可以使用 `OPENAI_API_KEY`、`OPENAI_MODEL` 和 `OPENAI_BASE_URL`。不要把真实
Key 写进 README、代码或提交到 Git。具体模型名和地址应以供应商文档为准。

## 运行

在仓库根目录进入持续多轮会话：

```powershell
python agent.py --workspace E:\你的\项目目录
```

出现 `你>` 后输入任务；当前任务完成后会继续等待下一条消息：

```text
你> 先读取项目并实现一个随机数生成器
你> 给它补充边界测试，并说明你修改了什么
你> /exit
```

会话命令：

- `/help`：显示命令帮助。
- `/new`：清空内存中的对话历史，但不回滚已经修改的文件。
- `/exit`：结束会话并返回 PowerShell。
- `Ctrl+C`：在等待输入时退出；任务执行期间用于中断当前任务。

如果命令行中直接给出任务，默认仍是单次运行：

```powershell
python agent.py --workspace E:\你的\项目目录 "实现一个带测试的随机数生成器"
```

若希望首个任务完成后继续对话，增加 `--interactive`：

```powershell
python agent.py --workspace E:\你的\项目目录 --interactive "先分析现有代码"
```

标准输入管道保持单次运行，便于脚本调用。查看全部参数：

```powershell
python agent.py --help
```

## 多轮与 Agent 循环的区别

一次“用户消息”内部可能包含多次模型请求：模型提出工具调用，本地执行工具，把真实结果
返回模型，直到模型给出不含工具调用的最终文本。这个过程是 Agent 循环。

一次任务完成后，CLI 不退出，而是保存该任务与最终回答，再等待下一条用户消息。后续任务
会携带这些已完成对话；失败或中断的任务不进入历史，避免把不可靠状态当成既定事实。历史
目前只保存在当前 Python 进程内，退出程序后不会持久化。

## 测试

```powershell
python -m unittest discover -s tests -t . -v
```

可选安装为命令：

```powershell
python -m pip install -e .
coding-agent --workspace E:\你的\项目目录
```

本项目不是系统级安全沙箱。请只对可控目录授权，并在提交代码前人工审查改动。

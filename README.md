# coding-agent

一个不依赖 Agent 框架的本地编程智能体。模型通过 OpenAI 兼容的 Chat Completions
接口返回原生 tool calling；工具的定义、参数校验、文件访问和命令执行均由本项目在本机完成。

## 当前范围

- 支持终端多轮会话，并把已成功完成的用户/助手消息传给后续任务。
- 支持读取、搜索、写入和修改工作区文件，以及执行受限制的本地命令。
- 支持 `inspect_code` 本地代码结构分析：Python 使用 AST 精确提取符号、接口、行号、文档和
  变量；其他语言提供明确标注为启发式的声明定位。
- 使用 ToolSpec 单一注册表生成工具 Schema、绑定本地处理函数并严格校验参数，不进行隐式类型转换。
- 使用 `finish_task` 证据完成门；修改文件后，必须引用最近修改之后真实成功的验证命令。
- 按工作区把每次模型 API 请求、响应和错误写入本地 JSONL，便于逐轮人工检查。
- 支持最大循环次数、重复工具调用熔断、命令超时、输出截断和路径越界拦截。
- 使用严格请求字符预算，同时计算消息、工具 Schema、JSON 包装和回复预留；超限时停止发送。
- 保留完整本地执行账本，对发给模型的视图按工具语义压缩，并保持 tool call/result 协议配对。
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

### 查看每轮 API 输入输出

默认记录目录为：

```text
.coding-agent/api-traces/
```

一个工作区对应一个稳定的 `.jsonl` 文件。每行是 `session_start`、`api_request`、
`api_response` 或 `api_error` 事件；请求和响应通过 `request_id` 对应。启动时终端会打印本次
项目使用的准确文件路径。也可以覆盖目录：

```powershell
python agent.py --workspace E:\你的\项目目录 --trace-dir E:\本地审计记录
```

记录器不会接收 HTTP Authorization/API Key，但文件会包含完整提示词、工具 Schema、模型
回复和可能出现的代码内容。`.coding-agent/` 已被 Git 忽略，仍应只在本机保存，不要加入视频
或手动上传。

## 多轮与 Agent 循环的区别

一次“用户消息”内部可能包含多次模型请求：模型提出工具调用，本地执行工具，把真实结果
返回模型。模型不能用普通文本直接结束，而要单独调用 `finish_task`。如果工作区发生修改，
完成门会核对其提交的验证命令是否在最后一次修改之后真实执行且退出码为 0；验证通过后才
生成最终结果。这个过程是 Agent 循环。

一次任务完成后，CLI 不退出，而是保存该任务与最终回答，再等待下一条用户消息。后续任务
会携带这些已完成对话；失败或中断的任务不进入历史，避免把不可靠状态当成既定事实。历史
目前只保存在当前 Python 进程内，退出程序后不会持久化。

## 上下文压缩与精确代码

每轮请求都重新从本地真实轨迹生成，而不是把上一轮请求继续向后追加。优先级大致是：系统
规则和当前任务、最近工具块与错误、较早对话。成功的 `write_file`/`replace_in_file` 历史参数
不会重复携带整段代码，而会替换为路径、长度和哈希；文件随后被修改时，旧 `read_file` 结果
会标记为失效。命令失败会保留退出码以及输出头尾。

需要处理长文件时，模型先调用 `inspect_code` 获得类、函数签名、行号、说明和变量，再用
`read_file(start_line, end_line)` 读取准备修改的精确范围。结构摘要是导航索引，不是代码真值，
也不会凭空推断没有文档的业务语义；实际修改前仍需读取磁盘上的当前代码。

`--context-chars` 是序列化后的字符安全预算，不是供应商精确 Token 计数。它计入工具 Schema、
请求包装并为模型回复预留空间；如果连必要内容都无法容纳，Agent 会明确报错，而不会发送已知
超限的请求。不同模型的 Token 切分和真实上下文上限仍以供应商为准。

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

# Coding Agent

一个不依赖 Agent 框架的轻量级编程智能体。模型通过 OpenAI 兼容的原生 tool calling 提出操作，本项目自行实现模型输出解析、对话历史与上下文压缩、本地工具执行、循环终止和错误恢复。

## Web UI

UI 使用 Python 标准库 HTTP 服务和原生 HTML/CSS/JavaScript，不需要安装前端或 Web 框架。

PowerShell 中先设置模型配置：

```powershell
$env:CODING_AGENT_API_KEY="你的 API key"
$env:CODING_AGENT_MODEL="支持 tool calling 的模型名"
$env:CODING_AGENT_BASE_URL="模型服务商的 OpenAI 兼容 v1 地址"
```

然后启动：

```powershell
python ui.py --open
```

浏览器默认打开 `http://127.0.0.1:8765`。左侧创建和选择项目，中间进行多轮对话，右侧查看模型轮次、工具参数摘要和本地执行结果。项目默认创建在 `workspaces/`，UI 状态保存在 `.coding-agent/ui-state.json`；两者均已被 Git 忽略，API Key 只从服务端进程的环境变量读取。

也可以安装后运行：

```powershell
python -m pip install -e .
coding-agent-ui --open
```

查看 UI 参数：

```powershell
python ui.py --help
```

## 命令行模式

不在命令行直接给出任务时，会进入持续多轮会话：

```powershell
python agent.py --workspace E:\path\to\project
```

```text
你> 创建一个随机数生成器并运行测试
你> 在刚才的项目中增加最小值和最大值输入
你> 再增加一个 README
你> /exit
```

同一进程中，成功完成的用户任务和最终回答会进入下一轮上下文；`/new` 清空对话上下文但不回滚工作区文件，`/exit` 退出。CLI 历史只保存在当前进程内，重启后需要使用 UI 才能恢复持久化历史。

需要执行一次任务后退出时，直接把任务写在命令后：

```powershell
python agent.py --workspace . "实现一个带测试的待办事项命令行程序"
```

需要先执行命令行任务再继续对话时使用：

```powershell
python agent.py --interactive --workspace . "先检查当前项目"
```

## Agent 运行链路

```text
用户任务与历史
      ↓
Conversation 按预算组织上下文
      ↓
ChatCompletionsClient 调用模型
      ↓
CodingAgent 解析文本或 tool_calls
      ↓
WorkspaceTools 校验并在本地执行
      ↓
结构化工具结果返回模型，继续下一轮
```

模型只负责推理、生成代码文本和选择工具。文件读写与命令执行均由本项目的本地工具完成，没有使用 Code Interpreter、Files API 或托管 Agent 工具。

循环在以下情况下停止：

- 模型返回非空最终文本且不再调用工具：正常完成。
- 连续三轮产生完全相同的工具调用：判定可能死循环并停止。
- 达到最大迭代次数：强制停止，控制时间和 Token 成本。
- 模型响应协议无效、API 重试仍失败或发生不可恢复错误：受控失败并显示原因。

历史只向模型提供已完成的“用户—助手”对话对；失败轮次保留在 UI 中用于审计，但不会污染后续模型上下文。当前任务的工具调用及其结果按完整协议块保存，空间不足时优先保留最近块并确定性压缩更早轨迹。

## 本地工具与安全边界

模型只能请求六个已注册工具：`list_files`、`read_file`、`write_file`、`replace_in_file`、`search_text` 和 `run_command`。工具层负责路径归一化、工作区边界、敏感文件保护、参数范围、文件大小、超时和输出截断。

本项目不是系统级沙箱。特别是 `run_command` 仍使用本机 Shell，危险命令黑名单不能覆盖所有绕过方式。只应在可控工作区运行，并在提交前审查改动。

## 测试

```powershell
python -m unittest discover -s tests -t . -v
```

技术实现和设计决策详见本地 `docs/UI技术设计与Agent运行机制.md`。`docs/` 按项目约定不提交到 Git 仓库。

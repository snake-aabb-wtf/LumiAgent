# LumiAgent

运行在本机的 **文件助手 AI Agent**（Textual TUI）。通过 OpenAI 兼容的 Chat Completions API + **Function Calling**，
对你的**工作根目录**下的文件进行增删改查：

- 读取文件（`read_file`，支持行号、分段）
- 创建 / 覆盖（`write_file`）、追加（`append_file`）
- 删除文件（`delete_file`）
- 列出目录（`list_directory`，支持递归）、新建目录（`create_directory`）
- 重命名 / 移动（`rename_file`）、文件信息（`get_file_info`）

**工作根目录**：源码运行时 = 项目根目录（`main.py` 所在目录）；未来打包为 exe 后 = exe 所在目录。

> ⚠️ 安全说明：当前版本**未实现文件操作安全策略**，Agent 拥有对根目录（以及它传入的任何路径）的读写权限。请只连接你信任的模型服务，不要在不信任的环境中使用。

## 环境要求

- Python 3.10+
- Windows / Linux / macOS 均可（TUI 需要支持 ANSI 的终端，Windows 推荐 Windows Terminal）

## 安装与运行

```bash
pip install -r requirements.txt
python main.py          # 或 python -m lumiagent，Windows 也可双击 run.bat
```

首次运行会在根目录生成 `config.json`（默认 `base_url=https://api.openai.com/v1`）。

## 配置

支持任意 OpenAI 兼容服务：OpenAI、DeepSeek、Qwen（通义千问）、Moonshot、本地 Ollama / vLLM / LM Studio 等。

两种配置方式（修改后立即保存到 `config.json`，无需重启）：

1. **配置面板**：TUI 内按 `Ctrl+P` 填写 base_url / api_key / model
2. **模型面板**：TUI 内按 `Ctrl+M` 进入 `/model` 面板，内置 OpenAI、DeepSeek、通义千问、Moonshot Kimi、智谱 GLM、本地 Ollama 等预设；选中即填充 base_url + 模型名，可在下方继续微调后保存
3. **命令**：`/set <项> <值>`，例如
   ```
   /set base_url https://api.deepseek.com
   /set api_key sk-xxxxxxxx
   /set model deepseek-chat
   ```
   其他可选配置项：`stream`（true/false）、`temperature`（0~2）、`max_tokens`（正整数，可设 `none` 清除）、`max_retries`（0~10，瞬时故障重试次数，默认 3）

> 本地服务（如 Ollama `http://localhost:11434/v1`）可以留空 api_key。

## 计划模式（Plan Mode）

LumiAgent 的文件操作有副作用（增删改文件）。计划模式让你在 Agent 真正动文件前先看到它打算做哪些操作，逐一批准后才放行。

- `/plan` 切换开关；状态栏出现 `⊕计划模式` 标记
- 每轮工具调用前，Agent 暂停并弹出审批面板，列出拟执行的工具与参数；`Enter` 批准 / `Esc` 拒绝
- 批准 → 执行；拒绝 → 把“用户拒绝了该计划”回传给模型，它会调整方案或改为直接回复
- 仅对工具调用生效；纯文本回复不受影响
- 与前缀缓存兼容：拒绝时不把含 `tool_calls` 的 assistant 消息入历史，避免悬挂 tool_calls 冲击缓存前缀

## TUI 使用

| 操作 | 说明 |
| --- | --- |
| 输入消息 + `Enter` | 与 Agent 对话，Agent 会按需调用文件工具 |
| `/help` | 命令帮助 |
| `/clear` | 清空当前对话窗口（不影响会话文件） |
| `/new` | 新建会话（清空当前对话，不落盘） |
| `/session` | 会话管理面板（载入 / 重命名 / 删除） |
| `/config` | 配置面板 |
| `/model` | 选择模型 / 提供商面板（预设 + 自定义） |
| `/plan` | 切换计划模式（开启后每次文件操作需先批准） |
| `/set <项> <值>` | 快速设置配置项 |
| `/root` | 显示工作根目录 |
| `/tools` | 列出全部工具 |
| `/quit` | 退出 |
| `Ctrl+C` | 停止当前生成 |
| `Alt+M` | 打开模型 / 提供商面板 |
| `Ctrl+L` / `Ctrl+Q` | 清空对话 / 退出 |

对话示例：

```
你: 帮我看看根目录下都有什么文件
你: 新建一个 notes/ideas.md，写上三条想法
你: 把 notes/ideas.md 里的第二条改成“用 Rust 重写”
你: 删除 notes/ideas.md
```

## 项目结构

```
main.py                   # 入口
run.bat                   # Windows 快捷启动
config.json               # 运行后生成（含 api_key，注意保管）
lumiagent/
  rootdir.py              # 工作根目录定位（兼容源码 / 打包 exe）
  config.py               # 配置加载 / 保存 / 校验
  tools.py                # 8 个文件工具 + JSON Schema + 执行器
  llm.py                  # OpenAI 兼容 HTTP 客户端（流式 + tool calling）
  agent.py                # Agent 主循环（消息管理 + 工具循环）
  session_store.py        # 会话持久化（~/.LumiAgent/sessions）
  tui.py                  # Textual TUI
tests/
  smoke_test.py           # 工具层 + Mock LLM 端到端冒烟测试
  tui_smoke.py            # TUI 无头冒烟测试
```

## 测试

```bash
python tests/smoke_test.py    # 不依赖真实 API，内置 Mock 服务验证 Function Calling 全流程
python tests/tui_smoke.py     # TUI 无头冒烟测试
```

## 提示词与前缀缓存

LumiAgent 的请求结构对前缀缓存（prefix cache，DeepSeek/OpenAI 均自动支持）友好：

```
[ system 提示词 ] + [ tools JSON ] + [ 消息历史… ] + [ 本轮新内容 ]
├── 静态前缀（缓存常驻）──┤├─ 每轮追加（命中前轮缓存）─┤└─ 必变 ─┘
```

三条不变式（代码内注释 + `tests/smoke_test.py` 第 5 组回归测试双重护栏）：

1. 系统提示词在进程内生成一次后不再修改，始终位于 `messages[0]`；
2. `TOOL_SCHEMAS` 是模块常量，逐字节稳定，不动态重建；
3. 消息历史 append-only，不回改旧消息（未来做历史裁剪时必须从尾部裁、头部不动）。

效果：第 N 轮请求可命中第 N-1 轮的全部前缀，长对话下输入 token 费用显著下降（DeepSeek 命中部分按缓存价计费）。

本仓库只含源码与会话目录的**隔离约定**：config 与会话都不入库。

## 会话存储

对话历史会自动保存到本机：

```
~/.LumiAgent/sessions/   （Windows: C:\Users\<你>\.LumiAgent\sessions）
└── <8位id>.json
```

- **首次发送消息**时自动创建会话并落盘；每轮对话结束后自动覆盖更新（保持 append-only 头部，前缀缓存不受影响）。
- `/session` 打开会话管理面板：↑/↓ 选择、Enter 载入、R 重命名、D 删除、Esc 返回；也可点击行选中。
- `/new` 新建会话；`/clear` 仅清空当前显示窗口、不删除会话文件。
- 会话文件只存对话内容（system + turns），不含 api_key 等敏感信息；可手动编辑 / 备份 / 删除整个目录。
- 载入旧会话时若系统提示词与当前不一致，会自动保留当前稳定头部、丢弃旧的 system 消息，保证前缀缓存友好。

## 说明与后续

- 对话历史已持久化到 `~/.LumiAgent/sessions`，重启后可用 `/session` 载入继续。
- 打包成 exe（如 PyInstaller）后，根目录自动切换为 exe 所在目录，无需改代码；会话目录仍在用户家目录下，跨工作目录保留。
- 安全策略（路径白名单、危险操作确认等）预留了位置，后续版本可加。
- 系统提示词设计原则：无时间戳/随机数等“缓存毒药”；工作流约束（先读后写、并行调用、大文件分段）写进静态前缀，一次缓存长期受益。

## 运行时韧性（harness 打磨点）

- **API 重试**：网络错误 / 超时 / HTTP 5xx / 429 会按指数退避自动重试，次数由 `max_retries` 控制；非瞬时 4xx 不重试，直接降级（流式→非流式、去掉 tools）后上抛。
- **二进制守卫**：`read_file` 嗅探开头字节，疑似二进制（图片/压缩包等）时拒绝，避免把乱码塞进上下文。
- **滚动尊重**：你向上翻看历史时，流式新内容不会把视图拽回底部；只在靠近底部时才自动跟随。
- **状态可见**：状态栏显示当前对话轮数，便于判断何时该 `/clear` 或 `/new` 起新会话。
- **工具调用前的解释不丢失**：模型在调用工具前通常先说一句（如“我先看看根目录”），这句会显示给你，而不是静默吞掉。

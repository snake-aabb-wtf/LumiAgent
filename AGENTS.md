# AGENTS.md

本文件给 AI 编码 Agent 与协作者提供在 LumiAgent 仓库内工作所需的最小上下文。
**修改任何代码前先读“不可违反的约定”。**

## 项目概览

LumiAgent 是一个运行在本机的文件助手 AI Agent：

- **TUI**：Textual（`lumiagent/tui.py`），无头测试用 `run_test()`（Textual Pilot）
- **LLM 接入**：纯 HTTP（httpx）直连任意 OpenAI 兼容 `/chat/completions`，流式 SSE + Function Calling
- **功能**：对“工作根目录”下的文件做增删改查（8 个工具）、计划模式（工具调用需批准）、会话持久化、配置面板、模型提供商预设
- **工作根目录**：源码运行 = 项目根目录（`main.py` 所在处）；打包 exe 后 = exe 所在目录（见 `rootdir.py`）
- 依赖仅两个：`textual`、`httpx`（见 `requirements.txt`）

## 常用命令

```bash
python main.py                    # 启动 TUI
python -m lumiagent               # 等价入口
python tests/smoke_test.py        # 核心 + Mock 端到端测试（不依赖真实 API）
python tests/tui_smoke.py         # TUI 无头冒烟测试
python -m compileall -q lumiagent tests   # 改完先编译检查
```

CI：`.github/workflows/tests.yml`（push/PR 触发，ubuntu + py3.12，两个测试脚本都跑）。

## 架构速览

| 模块 | 职责 |
| --- | --- |
| `lumiagent/agent.py` | Agent 主循环：消息管理 + 工具循环（上限 `MAX_TOOL_ROUNDS=12`），与 UI 解耦的事件回调 |
| `lumiagent/llm.py` | OpenAI 兼容客户端：流式/非流式、tool_calls 增量合并、瞬时故障指数退避重试、两条降级（流式→非流式、去 tools） |
| `lumiagent/tools.py` | 8 个文件工具 + JSON Schema（`TOOL_SCHEMAS`）+ 执行器 `execute_tool()` |
| `lumiagent/tui.py` | Textual 应用：对话流、`/` 命令、配置/模型/会话/审批面板、状态栏 |
| `lumiagent/config.py` | `config.json` 配置（base_url / api_key / model / stream / temperature / max_tokens / max_retries） |
| `lumiagent/session_store.py` | 会话持久化到 `~/.LumiAgent/sessions/<8位id>.json` |
| `lumiagent/rootdir.py` | 工作根目录定位（兼容源码 / 打包 exe） |

**Agent → TUI 事件协议**（`agent.py` 顶部有权威文档，改动必须同步两处）：
`("user", text)`、`("assistant_chunk", text)`、`("assistant", text)`、`("tool_round", n, count)`、
`("tool", name, raw_args, ok, result)`、`("plan_ready", n, calls)`、`("plan_approved", n)`、
`("plan_rejected", n)`、`("error", msg)`。
`agent.chat()` 另接受 `approver`（异步回调，返回 bool）用于计划模式审批。

## 不可违反的约定

### 1. 前缀缓存不变式（prefix cache）
DeepSeek/OpenAI 按 token 级最长公共前缀自动缓存。以下任何一条被破坏都会让缓存命中归零：

1. **系统提示词**（`SYSTEM_PROMPT_TEMPLATE`）在 `Agent.__init__` 中 format 一次后永不修改，始终位于 `messages[0]`；**禁止**往里面放时间戳、随机数、运行期状态。
2. **`TOOL_SCHEMAS` 是模块常量**，每次请求原样下发、逐字节稳定；禁止按请求动态重建/重排/增删字段。
3. **消息历史 append-only**：只追加、绝不回改旧消息；未来做历史裁剪时必须从尾部裁、头部不动。
4. `session_store` 落盘/载入也遵守同一条：载入旧会话时头部不一致就用当前稳定系统提示词替换（见 `Agent.replace_messages`）。

### 2. 路径语义：全部相对工作根目录（无例外）
`tools._resolve(root, raw)`：空路径→根目录；其余**一律 strip 前导 `/` 和 `\` 后相对根目录解析**。
- `/d.txt`、`\d.txt`、`d.txt` 等价；Windows 盘符 `C:\foo` 也按相对（`root/C:/foo`）。
- **不要**用 `Path(raw).is_absolute()` 判断——Linux 上 `/etc/x` 会被误当系统绝对路径（这是历史踩过的跨平台 bug）。
- 目前无安全策略（用户明确要求暂不做），不要在工具里加路径白名单。

### 3. 计划模式（plan mode）
- `agent.plan_mode` 是运行期开关，**不得写进系统提示词**（会破坏不变式 1）。
- `approver` 返回 False 时：**不得**把含 `tool_calls` 的 assistant 消息入历史（避免悬挂 tool_calls 没有对应 tool 结果，破坏后续请求），改为追加一条“用户拒绝”的 user 消息让模型调整。
- TUI 侧 `_approve_plan` 用 `asyncio.Future` + `push_screen` 阻塞等待审批面板。

### 4. 测试隔离
- 测试**必须**把 `session_store.SESSIONS_DIR` 指到临时目录（`tmp / ".LumiAgent" / "sessions"`），禁止污染真实 `~/.LumiAgent`。
- 测试不得依赖真实 API key；用 `ThreadingHTTPServer` Mock 兼容服务（SSE）。
- Windows GBK 控制台下打印 emoji：测试文件顶部有 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，新测试脚本也要加。

### 5. 编码与其它
- 所有文件 UTF-8；工具写文件一律 UTF-8。
- `read_file` 有二进制嗅探守卫（含 NUL 或不可打印比例过高 → 拒绝），不要绕过。
- 界面自动滚动用 `_maybe_scroll()`（尊重用户翻看历史时的滚动位置），不要直接 `scroll_end`。
- 文案与交互用中文，与 README 保持一致。

## 常见修改任务清单

**加一个新工具**（`tools.py`）：
1. 实现 `def handler(root: Path, **args) -> str`（返回给人/模型的文本；出错 raise，执行器会转成 `(False, 错误文本)`）；
2. 用 `_schema(name, desc, properties, required)` 追加到 `TOOL_SCHEMAS`；
3. 注册进 `_TOOL_HANDLERS`；
4. 在 `tests/smoke_test.py` 的 `test_tools` 补断言。

**改系统提示词**：
- 只改静态文本；`{root}` 是唯一允许的插值；改完跑 `tests/smoke_test.py` 第 5 组（缓存友好性回归）。

**加一个新 Agent 事件**：
- `agent.py` 事件文档注释里声明格式 → `agent.py` 里 `emit(...)` → `tui.py` `_handle_event` 加分支。

**加一个 TUI 命令**：
- `tui.py` `_run_command` 加分支 → `HELP_TEXT` 加行 → 有需要时 `tui_smoke.py` 补验证。

**改 HTTP 客户端行为**（`llm.py`）：
- 瞬时故障（网络/超时/5xx/429）走 `_request()` 退避重试；非瞬时 4xx 上抛后由 `chat()` 决定两条降级（流式→非流式、去 tools）。不要绕过降级链。

## 验证流程（改完必跑）

```bash
python -m compileall -q lumiagent tests
python tests/smoke_test.py     # 全绿再提交
python tests/tui_smoke.py
```

CI 会在 push/PR 时用 ubuntu + py3.12 再跑一遍同样的两个脚本——**本地能过但 CI 红，通常是路径/编码/大小写等跨平台差异，优先怀疑 `_resolve` 与文件路径处理**。

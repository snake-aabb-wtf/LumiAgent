"""LumiAgent 的 Textual TUI 界面。

用法：
- 底部输入框输入消息，Enter 发送
- 以 / 开头的命令：/help /clear /config /set /root /tools /quit
- 快捷键：Ctrl+P 配置，Ctrl+C 停止生成，Ctrl+L 清空，Ctrl+Q 退出
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .agent import Agent
from .config import Config
from . import session_store

CSS = """
Screen { background: #0e141b; }

#conversation {
    height: 1fr;
    border: round #334155;
    background: #101823;
    padding: 1 2;
    margin: 0 1;
}
#conversation:focus-within { border: round #3b82f6; }

#status-bar { height: 1; background: #1c2532; color: #94a3b8; }
#status-bar Static { padding: 0 2; }

#chat-input { height: 3; margin: 0 1 1 1; border: round #334155; }
#chat-input:focus { border: round #3b82f6; }
#chat-input:disabled { opacity: 0.5; }

.user-msg    { color: #a5d8ff; background: #14263b; padding: 0 1; margin-bottom: 1; }
.tool-msg    { color: #8b98a9; background: #1b232e; padding: 0 1; margin-bottom: 1; }
.error-msg   { color: #ff8a80; background: #331c1c; padding: 0 1; margin-bottom: 1; }
.hint-msg    { color: #7d8590; margin-bottom: 1; }
.assistant-msg { margin-bottom: 1; }

ConfigScreen { background: #0e141b; padding: 1 3; }
#cfg-title { text-style: bold; color: #7dd3fc; margin-bottom: 1; }
ConfigScreen Label { color: #94a3b8; margin-top: 1; }
#cfg-actions { height: 3; margin-top: 2; }
#cfg-save { margin-right: 2; }

ModelScreen { background: #0e141b; padding: 1 3; }
#model-title { text-style: bold; color: #7dd3fc; margin-bottom: 0; }
#model-subtitle { color: #7d8590; margin-bottom: 1; }
#model-presets { height: 8; border: round #334155; margin-bottom: 1; }
ModelScreen Label { color: #94a3b8; margin-top: 1; }
#model-actions { height: 3; margin-top: 2; }
#model-save { margin-right: 2; }

SessionScreen { background: #0e141b; padding: 1 3; }
#session-title { text-style: bold; color: #7dd3fc; margin-bottom: 1; }
#session-subtitle { color: #7d8590; margin-bottom: 1; }
#session-list { height: 1fr; border: round #334155; padding: 1; margin-bottom: 1; }
.session-row { height: 3; }
.session-row .label { color: #e6edf3; }
.session-row .meta { color: #7d8590; }
.session-row:hover { background: #16263b; }
.session-row.selected { background: #1f3a5f; border-left: thick #3b82f6; }
.session-empty { color: #7d8590; padding: 1; }
#session-actions { height: 3; }
#session-rename { margin-right: 2; }
"""

# 运行期会话状态（与 TUI 主框架绑定）
@dataclass
class AgentState:
    title: str = "(新会话)"
    session_id: str | None = None
    created: float | None = None

WELCOME = """## 👋 欢迎使用 LumiAgent

我是运行在本机的**文件助手 Agent**，通过函数调用（Function Calling）在**工作根目录**下执行文件操作：

- 读取 / 创建 / 写入 / 追加 / 删除文件
- 列出目录、新建目录、重命名 / 移动、查看文件信息

**工作根目录**：`{root}`

> ⚠️ 当前版本**没有文件安全策略**：Agent 拥有对根目录（及传入的任何路径）的读写权限，请只连接你信任的模型服务。

### 快速开始
1. 按 `Ctrl+P` 打开配置面板，填入 `base_url` / `api_key` / `model`（支持 OpenAI、DeepSeek、Qwen、本地 Ollama 等任意兼容服务）
2. 在下方输入框直接对话，例如：*“列出根目录下所有文件”*、*“把 notes.txt 的内容改成……”*
3. 输入 `/help` 查看全部命令
"""

HELP_TEXT = """## 命令帮助

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示本帮助 |
| `/clear` | 清空当前对话窗口（不清除会话历史文件） |
| `/new` | 新建会话（清空当前对话并另起一页） |
| `/session` | 打开会话管理面板（载入 / 重命名 / 删除历史会话） |
| `/config` | 打开配置面板（base_url / api_key / model） |
| `/model` | 选择模型 / 提供商面板（预设 + 自定义） |
| `/plan` | 切换计划模式（开启后每次文件操作需先批准） |
| `/set <项> <值>` | 快速设置并保存配置项：base_url / api_key / model / stream / temperature / max_tokens |
| `/root` | 显示工作根目录 |
| `/tools` | 显示可用工具列表 |
| `/quit` | 退出 LumiAgent |

**快捷键**：`Enter` 发送 · `Ctrl+P` 配置 · `Alt+M` 模型 · `Ctrl+C` 停止生成 · `Ctrl+L` 清空 · `Ctrl+Q` 退出
"""


# 提供商预设：(id, 显示名, base_url, 示例模型)
PROVIDER_PRESETS: list[tuple[str, str, str, str]] = [
    ("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-chat"),
    ("qwen", "Qwen 通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("moonshot", "Moonshot Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("glm", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("ollama", "本地 Ollama", "http://localhost:11434/v1", "qwen2.5:7b"),
    ("custom", "自定义", "", ""),
]


class ModelScreen(Screen[dict[str, str] | None]):
    """模型 / 提供商面板：内置常见预设，选中即填充 base_url + 模型，可继续微调。"""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._cfg = config

    def compose(self) -> ComposeResult:
        yield Static("🧠  选择模型 / 提供商", id="model-title")
        yield Static("从预设中选择一枚提供商，可在下方输入框继续微调", id="model-subtitle")
        yield Label("提供商预设")
        yield OptionList(
            *[
                Option(f"{label}  —  {url or '（自定义）'}", id=slug)
                for slug, label, url, model in PROVIDER_PRESETS
            ],
            id="model-presets",
        )
        yield Label("Base URL")
        yield Input(value=self._cfg.base_url, placeholder="https://... 或 http://localhost:11434/v1", id="model-base-url")
        yield Label("API Key（留空表示不修改；本地服务可留空）")
        yield Input(value=self._cfg.api_key, password=True, id="model-api-key")
        yield Label("模型")
        yield Input(value=self._cfg.model, placeholder="模型名", id="model-model")
        with Horizontal(id="model-actions"):
            yield Button("保存", variant="primary", id="model-save")
            yield Button("取消", id="model-cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        slug = event.option_id
        for s, label, url, model in PROVIDER_PRESETS:
            if s == slug:
                url_input = self.query_one("#model-base-url", Input)
                model_input = self.query_one("#model-model", Input)
                url_input.value = url
                model_input.value = model
                if slug == "ollama":
                    # 本地服务无需 key，清空便于确认
                    self.query_one("#model-api-key", Input).value = ""
                break

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "model-save":
            self.dismiss(
                {
                    "base_url": self.query_one("#model-base-url", Input).value.strip(),
                    "api_key": self.query_one("#model-api-key", Input).value.strip(),
                    "model": self.query_one("#model-model", Input).value.strip(),
                }
            )
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigScreen(Screen[dict[str, str] | None]):
    """配置面板：base_url / api_key / model。"""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._cfg = config

    def compose(self) -> ComposeResult:
        yield Static("⚙️  配置 API（修改后立即保存到 config.json）", id="cfg-title")
        yield Label("Base URL")
        yield Input(value=self._cfg.base_url, placeholder="https://api.openai.com/v1", id="cfg-base-url")
        yield Label("API Key（留空表示不修改）")
        yield Input(value=self._cfg.api_key, password=True, id="cfg-api-key")
        yield Label("模型")
        yield Input(value=self._cfg.model, placeholder="gpt-4o-mini", id="cfg-model")
        with Horizontal(id="cfg-actions"):
            yield Button("保存", variant="primary", id="cfg-save")
            yield Button("取消", id="cfg-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-save":
            self.dismiss(
                {
                    "base_url": self.query_one("#cfg-base-url", Input).value.strip(),
                    "api_key": self.query_one("#cfg-api-key", Input).value.strip(),
                    "model": self.query_one("#cfg-model", Input).value.strip(),
                }
            )
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionScreen(Screen[dict[str, Any] | None]):
    """会话管理面板：载入 / 重命名 / 删除已保存会话。"""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._sessions: list[dict[str, Any]] = []
        self._current_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("🗂️  会话管理", id="session-title")
        yield Static("↑/↓ 选择 · Enter 载入 · R 重命名 · D 删除 · Esc 返回", id="session-subtitle")
        with VerticalScroll(id="session-list"):
            yield Label("（暂无会话）", classes="session-empty", id="session-empty")
        with Horizontal(id="session-actions"):
            yield Button("载入", variant="primary", id="session-load")
            yield Button("重命名", id="session-rename")
            yield Button("删除", variant="error", id="session-delete")
            yield Button("取消", id="session-cancel")

    async def on_mount(self) -> None:
        await self._refresh_list()

    async def _refresh_list(self) -> None:
        self._sessions = session_store.load_all()
        lst = self.query_one("#session-list", VerticalScroll)
        await lst.remove_children()
        ids = {s["id"] for s in self._sessions}
        if self._current_id not in ids:
            self._current_id = self._sessions[0]["id"] if self._sessions else None
        if not self._sessions:
            await lst.mount(Label("（暂无会话）", classes="session-empty", id="session-empty"))
            return
        for i, item in enumerate(self._sessions):
            sid = item["id"]
            title = escape(item.get("title", "(无标题)"))
            meta = (
                f"{item.get('message_count', 0)} 条 · "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(item.get('updated', 0)))}"
            )
            row = Static(
                f"[{i + 1}] {title}\n    {meta}  ({sid})",
                classes=f"session-row row-{sid}",
            )
            await lst.mount(row)
        # 高亮当前选中行
        if self._current_id:
            try:
                lst.query_one(f".row-{self._current_id}", Static).add_class("selected")
            except Exception:
                pass

    def _current_index(self) -> int:
        if not self._current_id:
            return 0
        for i, s in enumerate(self._sessions):
            if s["id"] == self._current_id:
                return i
        return 0

    def on_click(self, event) -> None:
        node = getattr(event, "node", None) or getattr(event, "widget", None)
        classes = getattr(node, "classes", None)
        if not classes:
            return
        for sid in (s["id"] for s in self._sessions):
            if f"row-{sid}" in classes:
                self._current_id = sid
                break

    def on_key(self, event) -> None:
        if not self._sessions:
            return
        key = event.key
        if key == "up":
            i = max(0, self._current_index() - 1)
            self._current_id = self._sessions[i]["id"]
        elif key == "down":
            i = min(len(self._sessions) - 1, self._current_index() + 1)
            self._current_id = self._sessions[i]["id"]
        elif key == "enter":
            self._do_load()
        elif key == "r":
            self._do_rename()
        elif key == "d":
            self._do_delete()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "session-load":
            self._do_load()
        elif bid == "session-rename":
            self._do_rename()
        elif bid == "session-delete":
            self._do_delete()
        else:
            self.dismiss(None)

    def _do_load(self) -> None:
        if not self._current_id:
            return
        data = session_store.load(self._current_id)
        if not data:
            self.app.notify(f"会话 {self._current_id} 读取失败", severity="error", timeout=3)
            return
        self.dismiss({"action": "load", "session": data})

    def _do_rename(self) -> None:
        if not self._current_id:
            return
        item = next((s for s in self._sessions if s["id"] == self._current_id), None)
        if not item:
            return
        self.dismiss({"action": "rename", "id": self._current_id, "old_title": item.get("title", "")})

    def _do_delete(self) -> None:
        if not self._current_id:
            return
        sid = self._current_id
        session_store.delete(sid)
        self._current_id = None
        self.app.notify(f"已删除会话 {sid}", timeout=3)
        import asyncio

        asyncio.create_task(self._refresh_list())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalScreen(Screen[bool]):
    """计划模式审批面板：展示拟执行的工具调用，等待用户批准 / 拒绝。"""

    BINDINGS = [
        Binding("escape", "reject", "拒绝"),
        Binding("enter", "approve", "批准"),
    ]

    def __init__(self, plan_calls: list[dict[str, str]]) -> None:
        super().__init__()
        self._plan_calls = plan_calls

    def compose(self) -> ComposeResult:
        yield Static("📝 计划审批", id="cfg-title")
        yield Static("下方是 Agent 拟执行的工具调用，确认无误后再放行：", id="model-subtitle")
        with VerticalScroll(id="session-list"):
            for i, c in enumerate(self._plan_calls, start=1):
                name = c.get("name", "?")
                raw = c.get("arguments", "{}")
                yield Static(f"[{i}] {name}({raw})", classes="hint-msg")
        with Horizontal(id="model-actions"):
            yield Button("批准 (Enter)", variant="primary", id="plan-approve")
            yield Button("拒绝 (Esc)", variant="error", id="plan-reject")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "plan-approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class LumiApp(App):
    TITLE = "LumiAgent"
    SUB_TITLE = "文件助手 Agent"
    CSS = CSS

    BINDINGS = [
        Binding("ctrl+q", "quit_app", "退出"),
        Binding("ctrl+l", "clear_chat", "清空"),
        Binding("ctrl+p", "open_config", "配置"),
        Binding("alt+m", "open_model", "模型"),
        Binding("ctrl+c", "cancel_turn", "停止"),
    ]

    def __init__(self, agent: Agent, config_path: Path, state: AgentState) -> None:
        super().__init__()
        self.agent = agent
        self.config_path = config_path
        self.state = state
        self._busy = False
        self._turn_worker: Any = None
        self._pending_stream: Markdown | None = None
        self._stream_text = ""
        self._last_render = 0.0

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ScrollableContainer(id="conversation"):
            yield Markdown(WELCOME.format(root=self.agent.root), classes="assistant-msg")
        with Horizontal(id="status-bar"):
            yield Static(f"📁 {self.agent.root}", id="status-root")
            yield Static("", id="status-model")
            yield Static("● 就绪", id="status-state")
        yield Input(
            placeholder="输入消息，Enter 发送；/help 查看命令；Ctrl+P 配置 · Alt+M 模型",
            id="chat-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_status()
        self.query_one("#chat-input", Input).focus()

    # ------------------------------------------------------------------
    # 状态栏
    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        cfg = self.agent.config
        turns = sum(1 for m in self.agent.messages if m.get("role") == "user")
        model_line = f"模型: {cfg.model} · {cfg.base_url}  · 对话 {turns} 轮"
        if getattr(self.agent, "plan_mode", False):
            model_line += "  ⊕计划模式"
        self.query_one("#status-model", Static).update(model_line)
        ready, msg = cfg.api_ready()
        if not ready:
            self.query_one("#status-state", Static).update(f"⚠ {msg}")
        else:
            self.query_one("#status-state", Static).update("● 就绪")

    def _update_state(self, text: str) -> None:
        self.query_one("#status-state", Static).update(text)

    def _at_bottom(self, conv: ScrollableContainer) -> bool:
        """用户是否已靠近对话底部（用于决定是否自动滚到底）。"""
        try:
            return conv.scroll_y >= conv.max_scroll_y - 1.5
        except Exception:
            return True

    def _maybe_scroll(self, conv: ScrollableContainer) -> None:
        """只在用户当前靠近底部时才滚到底，避免翻看历史时被新内容拽走。"""
        if self._at_bottom(conv):
            conv.scroll_end(animate=False)

    @staticmethod
    def _pretty_args(raw: str | None) -> str:
        """把工具参数 JSON 美化为紧凑字符串（失败时退化为原样）。"""
        raw = raw or "{}"
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False)
        except Exception:
            return raw

    async def _approve_plan(self, plan_calls: list[dict[str, str]]) -> bool:
        """计划模式批准器：弹审批面板并阻塞等待用户决定。"""
        # 若上一条对话里已临时渲染过 pending_stream（极少出现），先丢弃避免误显示
        if self._pending_stream is not None and not self._stream_text:
            await self._pending_stream.remove()
            self._pending_stream = None
        self._update_state("⏸ 等待计划审批…")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        def on_result(approved: object) -> None:
            if not fut.done():
                fut.set_result(bool(approved))

        self.push_screen(ApprovalScreen(plan_calls), on_result)
        try:
            approved = await fut
        finally:
            self._update_state("● 就绪" if not approved else "⏳ 执行计划…")
        return approved

    # ------------------------------------------------------------------
    # 输入处理
    # ------------------------------------------------------------------
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        text = event.input.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._run_command(text)
            return
        if self._busy:
            self.notify("正在处理上一条消息，请稍候…", severity="warning", timeout=3)
            return
        self._start_turn(text)

    # ------------------------------------------------------------------
    # 对话回合（worker）
    # ------------------------------------------------------------------
    def _start_turn(self, text: str) -> None:
        self._busy = True
        self._pending_stream = None
        self._stream_text = ""
        self.query_one("#chat-input", Input).disabled = True
        self._update_state("⏳ 思考中…")
        self._turn_worker = self.run_worker(self._run_turn(text), name="agent-turn")

    async def _run_turn(self, text: str) -> None:
        try:
            await self.agent.chat(
                text,
                on_event=self._handle_event,
                approver=self._approve_plan,  # 计划模式时由它阻塞等待批准；非计划模式 agent 不会调用
            )
        except asyncio.CancelledError:
            await self._handle_event(("error", "⏹ 已停止生成。"))
            raise
        except Exception as e:  # noqa: BLE001 —— 兜底
            await self._handle_event(("error", f"发生内部错误: {type(e).__name__}: {e}"))
        finally:
            self._busy = False
            self._pending_stream = None
            self._stream_text = ""
            inp = self.query_one("#chat-input", Input)
            inp.disabled = False
            self._refresh_status()  # 状态栏含对话轮数，随历史增长刷新
            inp.focus()
            # 每轮结束后写盘（前缀缓存首部不变，写盘只追加尾部）
            if self.state.session_id:
                self._persist_current()

    # ------------------------------------------------------------------
    # Agent 事件 → 界面
    # ------------------------------------------------------------------
    async def _handle_event(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        conv = self.query_one("#conversation", ScrollableContainer)

        if kind == "user":
            text = str(event[1])
            # 第一条用户消息落到未命名会话 → 创建会话并清窗口
            if self.state.session_id is None:
                self._start_session_id()
            if self.state.title == "(新会话)":
                self.state.title = session_store.derive_title_from_message(text)
            await conv.mount(Static(escape(text), classes="user-msg"))
            # 仅在首次消息时落盘（创建会话文件 + 清屏标记之后的标题）
            self._persist_current()

        elif kind == "assistant_chunk":
            text = str(event[1])
            if self._pending_stream is None:
                self._pending_stream = Markdown("", classes="assistant-msg")
                self._stream_text = ""
                self._last_render = 0.0
                await conv.mount(self._pending_stream)
            self._stream_text = text
            now = time.monotonic()
            if now - self._last_render >= 0.05:
                self._pending_stream.update(text)
                self._last_render = now
                self._maybe_scroll(conv)

        elif kind == "assistant":
            text = str(event[1])
            if self._pending_stream is not None:
                self._pending_stream.update(text)
                self._pending_stream = None
            else:
                await conv.mount(Markdown(text, classes="assistant-msg"))
            self._stream_text = ""
            self._update_state("● 就绪")
            self._maybe_scroll(conv)

        elif kind == "tool_round":
            self._update_state(f"🔧 第 {event[1]} 轮工具调用（{event[2]} 个）…")

        elif kind == "plan_ready":
            round_no = event[1]
            plan_calls = event[2]
            # 在对话里展示拟执行计划（标注：等待批准）
            lines = [f"## 📝 计划（第 {round_no} 轮）", "", "拟执行以下操作：", ""]
            for i, c in enumerate(plan_calls, start=1):
                name = c.get("name", "?")
                raw = c.get("arguments", "{}")
                pretty = self._pretty_args(raw)
                if len(pretty) > 400:
                    pretty = pretty[:400] + " …"
                lines.append(f"{i}. **{name}**({pretty})")
            lines.append("", "审批窗口已弹出，或点击下方按钮批准 / 拒绝。")
            await conv.mount(Markdown("\n".join(lines), classes="hint-msg"))
            self._maybe_scroll(conv)

        elif kind == "plan_approved":
            await conv.mount(
                Static(escape(f"✅ 计划已批准（第 {event[1]} 轮），开始执行…"), classes="tool-msg")
            )
            self._maybe_scroll(conv)

        elif kind == "plan_rejected":
            await conv.mount(
                Static(escape(f"⛔ 计划已拒绝（第 {event[1]} 轮），回传反馈让模型调整。"), classes="error-msg")
            )
            self._maybe_scroll(conv)

        elif kind == "tool":
            if self._pending_stream is not None:
                if not self._stream_text:
                    await self._pending_stream.remove()
                self._pending_stream = None
                self._stream_text = ""
            _, name, raw_args, ok, result = event
            try:
                args_str = json.dumps(json.loads(raw_args), ensure_ascii=False)
            except Exception:
                args_str = str(raw_args)
            preview = str(result).replace("\n", " ⏎ ")
            if len(preview) > 300:
                preview = preview[:300] + " …"
            icon = "✅" if ok else "❌"
            line = f"{icon} 工具 {name}({args_str}) → {preview}"
            await conv.mount(Static(escape(line), classes="tool-msg"))
            self._maybe_scroll(conv)

        elif kind == "error":
            await conv.mount(Static(escape(str(event[1])), classes="error-msg"))
            self._update_state("● 就绪")
            self._maybe_scroll(conv)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    async def _run_command(self, raw: str) -> None:
        parts = raw[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        conv = self.query_one("#conversation", ScrollableContainer)

        if cmd in ("help", "?"):
            await conv.mount(Markdown(HELP_TEXT, classes="assistant-msg"))
        elif cmd == "clear":
            if self._busy:
                self.notify("生成中无法清空，请先按 Ctrl+C 停止", severity="warning", timeout=3)
                return
            await self._apply_clear()
        elif cmd == "new":
            await self._new_session()
        elif cmd == "session":
            if self._busy:
                self.notify("生成中无法管理会话，请先按 Ctrl+C 停止", severity="warning", timeout=3)
                return
            self.push_screen(SessionScreen(self.agent.config), self._on_session_result)
        elif cmd == "config":
            self.push_screen(ConfigScreen(self.agent.config), self._on_config_saved)
        elif cmd == "model":
            self.push_screen(ModelScreen(self.agent.config), self._on_config_saved)
        elif cmd == "plan":
            self.agent.plan_mode = not getattr(self.agent, "plan_mode", False)
            self._refresh_status()
            state = "开启" if self.agent.plan_mode else "关闭"
            await conv.mount(
                Static(
                    escape(f"🔄 计划模式已{state}：{'每次文件操作前需先批准' if self.agent.plan_mode else 'Agent 将直接执行工具调用'}"),
                    classes="hint-msg",
                )
            )
            self.notify(f"计划模式 {state}", timeout=2)
        elif cmd == "set":
            field, _, value = arg.partition(" ")
            if not field:
                await conv.mount(
                    Static(escape("用法: /set <base_url|api_key|model|stream|temperature|max_tokens> <值>"), classes="hint-msg")
                )
                return
            ok, msg = self.agent.config.set_field(field, value)
            if ok:
                self.agent.config.save(self.config_path)
                self._refresh_status()
            await conv.mount(Static(escape(("✅ " if ok else "❌ ") + msg), classes="tool-msg"))
        elif cmd == "root":
            await conv.mount(
                Static(escape(f"📁 工作根目录: {self.agent.root}"), classes="hint-msg")
            )
        elif cmd == "tools":
            lines = ["## 可用工具\n"]
            for schema in self.tools_schemas():
                fn = schema["function"]
                params = fn["parameters"]
                args = ", ".join(
                    p + ("?" if p not in params.get("required", []) else "")
                    for p in params["properties"]
                )
                lines.append(f"- **{fn['name']}({args})** — {fn['description']}\n")
            await conv.mount(Markdown("".join(lines), classes="assistant-msg"))
        elif cmd == "quit":
            self.exit()
        else:
            self.notify(f"未知命令: /{cmd}（/help 查看帮助）", severity="warning", timeout=3)

    def _on_config_saved(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        cfg = self.agent.config
        for field, value in values.items():
            if field == "api_key" and not value:
                continue  # 留空 = 不修改
            if value is not None:
                cfg.set_field(field, value)
        cfg.save(self.config_path)
        self._refresh_status()
        self.notify("配置已保存 ✅", timeout=3)

    def tools_schemas(self):  # 方便 /tools 展示
        from .tools import TOOL_SCHEMAS

        return TOOL_SCHEMAS

    # ------------------------------------------------------------------
    # 会话持久化
    # ------------------------------------------------------------------
    def _start_session_id(self) -> None:
        """分配当前会话的 id 与创建时间（首次发送消息时触发）。"""
        self.state.session_id = session_store.new_session_id()
        self.state.created = time.time()

    def _persist_current(self) -> None:
        """把当前对话历史写盘。无 session_id 时跳过。"""
        if not self.state.session_id:
            return
        session_store.save(
            self.state.session_id,
            self.agent.messages,
            title=self.state.title,
            created=self.state.created,
        )

    async def _new_session(self) -> None:
        """新建会话：重置 Agent 状态、清空窗口、不带 session_id。"""
        if self._busy:
            self.notify("生成中无法新建会话，请先按 Ctrl+C 停止", severity="warning", timeout=3)
            return
        conv = self.query_one("#conversation", ScrollableContainer)
        await conv.remove_children()
        await conv.mount(Markdown(WELCOME.format(root=self.agent.root), classes="assistant-msg"))
        # 重建 Agent 对话头（保留系统提示词 + 从零开始新会话）
        from .agent import SYSTEM_PROMPT_TEMPLATE

        self.agent.messages = [
            {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(root=self.agent.root)}
        ]
        self._pending_stream = None
        self._stream_text = ""
        self.state = AgentState()
        self.notify("已新建会话", timeout=2)

    async def _apply_clear(self) -> None:
        """/clear：仅清空显示窗口的内容，不影响会话历史文件。"""
        conv = self.query_one("#conversation", ScrollableContainer)
        await conv.remove_children()
        await conv.mount(Markdown(WELCOME.format(root=self.agent.root), classes="assistant-msg"))
        self._pending_stream = None
        self._stream_text = ""

    async def _load_session(self, session: dict[str, Any]) -> None:
        """载入一个会话：恢复历史 + 重建对话窗口渲染。"""
        msgs = session.get("messages", [])
        self.agent.replace_messages(msgs)
        self.state.session_id = session.get("id") or session_store.new_session_id()
        self.state.title = session.get("title", "(载入会话)")
        self.state.created = session.get("created")
        # 重新渲染对话窗口（保留系统提示词外的全部可见消息）
        conv = self.query_one("#conversation", ScrollableContainer)
        await conv.remove_children()
        await conv.mount(Markdown(WELCOME.format(root=self.agent.root), classes="assistant-msg"))
        for m in self.agent.messages:
            role = m.get("role")
            content = m.get("content")
            if role == "user" and content:
                await conv.mount(Static(escape(str(content)), classes="user-msg"))
            elif role == "assistant" and content:
                await conv.mount(Markdown(str(content), classes="assistant-msg"))
            # tool / system 消息不重绘成聊天气泡
        await conv.scroll_end(animate=False)

    def _on_session_result(self, result: dict[str, Any] | None) -> None:
        """SessionScreen 关闭后的回调分发。"""
        if not result:
            return
        action = result.get("action")
        if action == "load":
            session = result.get("session")
            if session:
                self.run_worker(self._load_session(session), name="session-load")
        elif action == "rename":
            sid = result.get("id")
            old = result.get("old_title", "")
            self._prompt_rename(sid, old)
        elif action == "delete":
            self.notify("已删除", timeout=2)

    def _prompt_rename(self, sid: str | None, old_title: str) -> None:
        """简易重命名：用 Input 弹窗。这里用一个内联 Screen 重命名。"""

        class RenameScreen(Screen[str | None]):
            BINDINGS = [Binding("escape", "cancel", "取消")]

            def __init__(self, old: str) -> None:
                super().__init__()
                self._old = old

            def compose(self) -> ComposeResult:
                yield Static("✏️  重命名会话", id="cfg-title")
                yield Input(value=self._old, placeholder="新标题", id="rename-input")
                with Horizontal(id="cfg-actions"):
                    yield Button("保存", variant="primary", id="rename-save")
                    yield Button("取消", id="cfg-cancel")

            def on_button_pressed(self, event: Button.Pressed) -> None:
                if event.button.id == "rename-save":
                    self.dismiss(self.query_one("#rename-input", Input).value.strip() or None)
                else:
                    self.dismiss(None)

            def action_cancel(self) -> None:
                self.dismiss(None)

        if not sid:
            return

        def on_rename(new_title: str | None) -> None:
            if not new_title:
                return
            data = session_store.load(sid)
            if data:
                session_store.save(
                    sid,
                    data.get("messages", []),
                    title=new_title,
                    created=data.get("created"),
                )
                if sid == self.state.session_id:
                    self.state.title = new_title
                self.notify("已重命名 ✅", timeout=2)

        self.push_screen(RenameScreen(old_title), on_rename)

    # ------------------------------------------------------------------
    # 快捷键动作
    # ------------------------------------------------------------------
    def action_quit_app(self) -> None:
        self.exit()

    async def action_clear_chat(self) -> None:
        if self._busy:
            self.notify("生成中无法清空，请先按 Ctrl+C 停止", severity="warning", timeout=3)
            return
        await self._apply_clear()

    def action_open_config(self) -> None:
        self.push_screen(ConfigScreen(self.agent.config), self._on_config_saved)

    def action_open_model(self) -> None:
        self.push_screen(ModelScreen(self.agent.config), self._on_config_saved)

    def action_cancel_turn(self) -> None:
        # 若审批面板正打开，先拒绝它（解除阻塞，回到就绪）
        if isinstance(self.screen, ApprovalScreen):
            self.screen.dismiss(False)
            return
        if self._busy and self._turn_worker is not None:
            self._turn_worker.cancel()
        else:
            self.exit()


def main() -> None:
    from .agent import Agent
    from .config import Config
    from .rootdir import get_root_dir

    root = get_root_dir()
    config_path = root / "config.json"
    config = Config.load(config_path)
    if not config_path.exists():
        config.save(config_path)
    session_store.ensure_dir()
    agent = Agent(config, root)
    state = AgentState()
    LumiApp(agent, config_path, state).run()


if __name__ == "__main__":
    main()

"""TUI 冒烟测试：用 Textual 的 run_test（无头模式）验证界面能正常挂载、
命令能执行、配置面板与会话面板能开合。

完全隔离在临时目录中：config 与会话目录都指向 tmp，不污染真实 ~/.LumiAgent。
运行：python tests/tui_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台下正常打印 emoji
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lumiagent.agent import Agent  # noqa: E402
from lumiagent.config import Config  # noqa: E402
from lumiagent import session_store  # noqa: E402
from lumiagent.tui import AgentState, LumiApp, ApprovalScreen, ModelScreen, SessionScreen  # noqa: E402


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lumi-tui-"))
    # 隔离会话存储到临时目录
    session_store.SESSIONS_DIR = tmp / ".LumiAgent" / "sessions"
    session_store.ensure_dir()
    config_path = tmp / "config.json"
    cfg = Config(base_url="http://127.0.0.1:1", api_key="", model="mock")
    agent = Agent(cfg, tmp)
    app = LumiApp(agent, config_path, AgentState())

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#chat-input") is not None
        assert app.query_one("#conversation") is not None
        assert app.query_one("#status-model") is not None
        print("✅ 界面挂载正常")

        inp = app.query_one("#chat-input")

        for cmd in ("/help", "/root", "/clear", "/new"):
            inp.value = cmd
            await inp.action_submit()
            await pilot.pause(0.2)
        print("✅ /help /root /clear /new 命令正常")

        inp.value = "/set model mock-model"
        await inp.action_submit()
        await pilot.pause(0.2)
        assert cfg.model == "mock-model"
        print("✅ /set 命令正常")

        # 配置面板开合
        app.action_open_config()
        await pilot.pause(0.2)
        app.pop_screen()
        await pilot.pause(0.2)
        print("✅ 配置面板开合正常")

        # /model 提供商面板：打开 → 选中预设 → 校验填充 → 关闭
        inp.value = "/model"
        await inp.action_submit()
        await pilot.pause(0.3)
        assert isinstance(app.screen, ModelScreen)
        from textual.widgets import Input as _Input, OptionList

        presets = app.screen.query_one("#model-presets", OptionList)
        # 高亮第 2 项（DeepSeek）并触发选择，校验 base_url 被填充
        presets.highlighted = 1
        presets.action_select()
        await pilot.pause(0.2)
        assert app.screen.query_one("#model-base-url", _Input).value == "https://api.deepseek.com"
        assert app.screen.query_one("#model-model", _Input).value == "deepseek-chat"
        print("✅ /model 预设填充正常")
        app.pop_screen()
        await pilot.pause(0.2)

        # 会话面板开合
        inp.value = "/session"
        await inp.action_submit()
        await pilot.pause(0.3)
        assert isinstance(app.screen, SessionScreen)
        app.pop_screen()
        await pilot.pause(0.2)
        print("✅ /session 会话面板开合正常")

        # /plan 切换计划模式 + 状态栏标记 + 审批面板开合
        inp.value = "/plan"
        await inp.action_submit()
        await pilot.pause(0.2)
        assert agent.plan_mode is True
        status_text = str(app.query_one("#status-model").content)
        assert "计划模式" in status_text
        print("✅ /plan 开启 + 状态栏标记正常")

        # 直接构造审批面板，模拟批准
        screen = ApprovalScreen([{"name": "read_file", "arguments": '{"path":"hi"}'}])
        app.push_screen(screen)
        await pilot.pause(0.2)
        assert isinstance(app.screen, ApprovalScreen)
        screen.action_approve()
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ApprovalScreen)
        print("✅ 计划审批面板开合正常")

        # 关掉计划模式
        inp.value = "/plan"
        await inp.action_submit()
        await pilot.pause(0.2)
        assert agent.plan_mode is False
        print("✅ /plan 关闭正常")

    print("TUI 冒烟测试通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
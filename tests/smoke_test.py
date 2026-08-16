"""LumiAgent 冒烟测试：工具层 + 配置 + Mock LLM 端到端。

不依赖真实 API：本地起一个假的 OpenAI 兼容服务（SSE 流式），
验证 Agent 的 Function Calling 循环能正确执行工具并把结果回传给模型。

运行：python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台下正常打印 emoji
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lumiagent.agent import Agent  # noqa: E402
from lumiagent.config import Config  # noqa: E402
from lumiagent.llm import LLMClient  # noqa: E402
from lumiagent import session_store  # noqa: E402
from lumiagent.tools import (  # noqa: E402
    TOOL_SCHEMAS,
    append_file,
    create_directory,
    delete_file,
    execute_tool,
    get_file_info,
    list_directory,
    read_file,
    rename_file,
    write_file,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name}  {detail}")


# ======================================================================
# Mock OpenAI 兼容服务
# ======================================================================
class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    seen_headers: list[dict] = []

    def do_POST(self):
        self.seen_headers.append(dict(self.headers.items()))
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        stream = body.get("stream", False)
        messages = body["messages"]
        last = messages[-1]

        if last.get("role") == "tool":
            # 工具结果已回传 → 输出最终答案（流式，分 3 段）
            self._send_sse(
                [
                    {"choices": [{"delta": {"content": "好的，"}, "index": 0}]},
                    {"choices": [{"delta": {"content": "文件内容是：Hello LumiAgent!"}, "index": 0}]},
                    {"choices": [{"delta": {"content": " 我已完成读取。"}, "index": 0}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
                ]
            )
        elif last.get("role") == "user" and "拒绝" in (last.get("content") or ""):
            # 计划模式：用户拒绝了计划 → 模型改为直接回复文本
            self._send_sse(
                [
                    {"choices": [{"delta": {"content": "好的，已按你的反馈取消操作。"}, "index": 0}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
                ]
            )
        elif stream:
            # 第一轮：先发一句解释（content），再返回 read_file 工具调用（参数分两段增量到达）
            self._send_sse(
                [
                    {"choices": [{"delta": {"content": "我先读取该文件。"}, "index": 0}]},
                    {"choices": [{"delta": {"role": "assistant", "tool_calls": [
                        {"index": 0, "id": "call_mock_1", "type": "function",
                         "function": {"name": "read_file", "arguments": ""}}]}, "index": 0}]},
                    {"choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": "{\"path\": \"hello.txt\"}"}}]}, "index": 0}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]},
                ]
            )
        else:
            self._send_json({"choices": [{"message": {"role": "assistant", "content": "（非流式路径）"}}]})

    def _send_sse(self, chunks: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def _send_json(self, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 静音
        pass


# ======================================================================
# 测试 1：配置读写
# ======================================================================
def test_config(tmp: Path) -> None:
    print("\n[1] Config")
    cfg_path = tmp / "config.json"
    cfg = Config(base_url="https://api.deepseek.com", api_key="sk-test", model="deepseek-chat", stream=True)
    cfg.save(cfg_path)
    loaded = Config.load(cfg_path)
    check("save/load 往返一致", loaded.base_url == "https://api.deepseek.com" and loaded.model == "deepseek-chat")

    ok, _ = loaded.set_field("base_url", "not-a-url")
    check("非法 base_url 被拒绝", not ok)
    ok, _ = loaded.set_field("temperature", "1.5")
    check("temperature 设置", ok and loaded.temperature == 1.5)
    ok, _ = loaded.set_field("stream", "false")
    check("stream 设置", ok and loaded.stream is False)
    ok, _ = loaded.set_field("max_retries", "5")
    check("max_retries 设置", ok and loaded.max_retries == 5)
    ok, _ = loaded.set_field("max_retries", "0")
    check("max_retries 可置 0", ok and loaded.max_retries == 0)
    ok, _ = loaded.set_field("max_retries", "99")
    check("max_retries 上限保护", not ok)
    ok, _ = loaded.set_field("max_retries", "-1")
    check("max_retries 拒绝负数", not ok)
    ok, _ = loaded.set_field("unknown_field", "x")
    check("未知字段被拒绝", not ok)

    bad = tmp / "bad.json"
    bad.write_text("{corrupted json", encoding="utf-8")
    check("损坏配置文件回退默认值", Config.load(bad).model == "gpt-4o-mini")


# ======================================================================
# 测试 2：8 个文件工具
# ======================================================================
def test_tools(root: Path) -> None:
    print("\n[2] 文件工具")

    r = write_file(root, "a/b.txt", "line1\nline2\nline3\n")
    check("write_file 创建+父目录", (root / "a/b.txt").is_file() and "已写入" in r)

    r = read_file(root, "a/b.txt")
    check("read_file 全量读取", "line1" in r and "line3" in r)

    r = read_file(root, "a/b.txt", offset=2)
    check("read_file 分段读取", "line2" in r and "line1" not in r)

    r = append_file(root, "a/b.txt", "line4\n")
    check("append_file 追加", (root / "a/b.txt").read_text(encoding="utf-8").endswith("line4\n"))

    r = get_file_info(root, "a/b.txt")
    check("get_file_info", "文件" in r and "大小" in r)

    r = list_directory(root, "a")
    check("list_directory", "b.txt" in r)

    r = create_directory(root, "x/y")
    check("create_directory", (root / "x/y").is_dir())

    r = rename_file(root, "a/b.txt", "a/c.txt")
    check("rename_file", (root / "a/c.txt").is_file() and not (root / "a/b.txt").exists())

    r = delete_file(root, "a/c.txt")
    check("delete_file", not (root / "a/c.txt").exists())

    # 二进制守卫：写入含 NUL 的字节，read_file 应拒绝而不是塞乱码
    (root / "bin.dat").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00IHDR" + b"\x00" * 64)
    ok, text = execute_tool(root, "read_file", '{"path": "bin.dat"}')
    check("二进制文件被拒绝", not ok and "二进制" in text)
    # 文本文件仍可正常读取
    write_file(root, "ok.txt", "hi\n中文")
    ok, text = execute_tool(root, "read_file", '{"path": "ok.txt"}')
    check("文本文件不受守卫影响", ok and "中文" in text)

    # 反斜杠 / 前导斜杠路径
    write_file(root, "d.txt", "x")
    r = read_file(root, "/d.txt")
    check("前导斜杠按根相对解析", "x" in r)
    r = read_file(root, "d.txt")
    check("普通相对路径", "x" in r)
    r = read_file(root, str(root / "d.txt"))
    check("绝对路径", "x" in r)

    # 异常路径
    ok, text = execute_tool(root, "read_file", "{bad json")
    check("坏 JSON 参数 → 失败", not ok and "JSON" in text)
    ok, text = execute_tool(root, "no_such_tool", "{}")
    check("未知工具 → 失败", not ok and "未知工具" in text)
    ok, text = execute_tool(root, "read_file", '{"path": "not_exist.txt"}')
    check("读不存在的文件 → 失败", not ok and "不存在" in text)
    ok, text = execute_tool(root, "delete_file", '{"path": "a"}')
    check("删除目录被拒绝", not ok and "目录" in text)


# ======================================================================
# 测试 3：LLM 客户端 URL 归一化
# ======================================================================
def test_endpoint() -> None:
    print("\n[3] LLM 端点")
    c = LLMClient(Config(base_url="http://127.0.0.1:9999/v1"))
    check("base_url 以 /v1 结尾", c._endpoint() == "http://127.0.0.1:9999/v1/chat/completions")
    c = LLMClient(Config(base_url="http://127.0.0.1:9999/chat/completions"))
    check("base_url 已是完整端点", c._endpoint() == "http://127.0.0.1:9999/chat/completions")
    c = LLMClient(Config(base_url="http://127.0.0.1:9999/"))
    check("末尾斜杠被去除", c._endpoint() == "http://127.0.0.1:9999/chat/completions")


# ======================================================================
# 测试 4：Agent 端到端（Mock 服务）
# ======================================================================
async def test_agent_e2e() -> None:
    print("\n[4] Agent 端到端（Mock LLM + Function Calling）")
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tmp = Path(tempfile.mkdtemp(prefix="lumi-e2e-"))
        (tmp / "hello.txt").write_text("Hello LumiAgent!", encoding="utf-8")
        cfg = Config(base_url=f"http://127.0.0.1:{server.server_address[1]}", api_key="sk-mock", model="mock-model")
        agent = Agent(cfg, tmp)
        events: list[tuple] = []
        async def on_event(e): events.append(e)

        await agent.chat("读取 hello.txt 的内容", on_event=on_event)

        kinds = [e[0] for e in events]
        check("触发 user 事件", "user" in kinds)
        check("触发 tool 事件且成功", any(e[0] == "tool" and e[3] for e in events))
        tool_events = [e for e in events if e[0] == "tool"]
        check("工具名是 read_file", tool_events and tool_events[0][1] == "read_file")
        check("工具结果包含文件内容", tool_events and "Hello LumiAgent" in tool_events[0][4])
        finals = [e[1] for e in events if e[0] == "assistant"]
        check("最终回复包含读取结果", finals and "Hello LumiAgent" in finals[-1])
        check("流式 chunk 事件到达", "assistant_chunk" in kinds)

        # 工具调用前的"解释"应当被展示给用户（而非静默吞掉）
        assistant_events = [str(e[1]) for e in events if e[0] == "assistant"]
        check("tool_calls 前的 content 被展示", any("我先读取该文件" in t for t in assistant_events))

        # 消息历史正确：assistant 消息含 content + tool_calls（preamble 不重复入历史）
        tc_assistant = [m for m in agent.messages if m.get("role") == "assistant" and m.get("tool_calls")]
        check("含 tool_calls 的 assistant 仍带 content", tc_assistant and tc_assistant[0].get("content") == "我先读取该文件。")

        # 消息历史结构正确
        roles = [m["role"] for m in agent.messages]
        check("历史含 system/user/assistant/tool", {"system", "user", "tool", "assistant"} <= set(roles))
        check("Authorization 头正确", any(h.get("Authorization") == "Bearer sk-mock" for h in MockHandler.seen_headers))
    finally:
        server.shutdown()
        server.server_close()


# ======================================================================
# 测试 5：前缀缓存（prefix cache）友好性回归护栏
# ======================================================================
def test_cache_friendly(root: Path) -> None:
    print("\n[5] 前缀缓存友好性")
    # 1) 系统提示词逐字节稳定：同 root 的不同实例必须一致
    a1 = Agent(Config(), root)
    a2 = Agent(Config(), root)
    check("同 root 的系统提示词逐字节一致", a1.messages[0]["content"] == a2.messages[0]["content"])

    # 2) tools schema 序列化稳定（模块常量，不得动态重建）
    s1 = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
    s2 = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
    check("TOOL_SCHEMAS 序列化稳定", s1 == s2)

    # 3) 历史 append-only：追加消息后 messages[0] 不受影响
    before = a1.messages[0]["content"]
    a1.messages.append({"role": "user", "content": "hi"})
    a1.messages.append({"role": "assistant", "content": "hello"})
    check("追加历史不影响系统提示词", a1.messages[0]["content"] == before)

    # 4) 系统提示词不含时间戳等缓存毒药
    now = datetime.now()
    poison = now.strftime("%Y-%m-%d") in before or now.strftime("%H:%M") in before
    check("系统提示词无时间戳", not poison)


# ======================================================================
# 测试 6：会话持久化（~/.LumiAgent/sessions，测试中隔离到临时目录）
# ======================================================================
def test_sessions(tmp: Path) -> None:
    print("\n[6] 会话持久化")
    # 把 session_store 指向临时目录，避免污染真实 ~/.LumiAgent
    session_store.SESSIONS_DIR = tmp / ".LumiAgent" / "sessions"
    session_store.ensure_dir()

    sid1 = session_store.new_session_id()
    sid2 = session_store.new_session_id()
    check("会话 id 8 位且互不相同", sid1 != sid2 and len(sid1) == 8 and sid1.isalnum())

    msg1 = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "帮我看看根目录"},
        {"role": "assistant", "content": "好的，已列出。"},
    ]
    rec = session_store.save(sid1, msg1)
    check("save 写入并派生标题", rec["title"].startswith("帮我看看根目录"))
    check("save 文件存在", (tmp / ".LumiAgent" / "sessions" / f"{sid1}.json").is_file())

    listing = session_store.load_all()
    check("load_all 能看到会话", any(s["id"] == sid1 for s in listing))
    check("message_count = 总消息 - 1", next(s for s in listing if s["id"] == sid1)["message_count"] == 2)

    loaded = session_store.load(sid1)
    check("load 还原消息条数", loaded and len(loaded["messages"]) == 3)
    check("load 保留 system 头", loaded and loaded["messages"][0]["role"] == "system")

    # Agent.replace_messages：头部一致时原样恢复
    agent = Agent(Config(), tmp)
    agent.replace_messages(loaded["messages"])
    check("replace_messages 还原历史条数", len(agent.messages) == 3)
    check("replace_messages 不破坏系统提示词", agent.messages[0]["content"] == agent.system_prompt())

    # 头部不一致时：保留当前系统提示词，丢弃旧的 system
    agent.replace_messages([{"role": "system", "content": "别的提示词"}, {"role": "user", "content": "hi"}])
    check("头部不一致时保留当前系统提示词", agent.messages[0]["content"] == agent.system_prompt())
    check("头部不一致时载入后续对话", len(agent.messages) == 2 and agent.messages[1]["role"] == "user")

    check("delete 删除会话", session_store.delete(sid1) and not session_store.load(sid1))

    # 标题派生
    check("标题截断 + 省略号", session_store.derive_title_from_message("一" * 50).endswith("…"))


# ======================================================================
# 测试 7：计划模式（plan mode）—— Mock LLM 端到端
# ======================================================================
async def test_agent_plan_mode() -> None:
    print("\n[7] 计划模式")
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tmp = Path(tempfile.mkdtemp(prefix="lumi-plan-"))
        (tmp / "hello.txt").write_text("Hello LumiAgent!", encoding="utf-8")
        cfg = Config(base_url=f"http://127.0.0.1:{server.server_address[1]}", api_key="sk-mock", model="mock-model")
        agent = Agent(cfg, tmp)
        agent.plan_mode = True

        # 情况 A：批准 → 工具被执行
        events: list[tuple] = []
        approvals = []

        async def approve(calls):
            approvals.append(calls)
            return True

        async def on_event(e):
            events.append(e)

        await agent.chat("读取 hello.txt 的内容", on_event=on_event, approver=approve)
        check("批准触发 plan_ready", any(e[0] == "plan_ready" for e in events))
        check("批准触发 plan_approved", any(e[0] == "plan_approved" for e in events))
        check("批准后执行了工具", any(e[0] == "tool" and e[3] for e in events))
        check("拟计划含 read_file", approvals and approvals[0][0]["name"] == "read_file")
        check("拒绝路径不残留悬挂 tool_calls", not any(m.get("tool_calls") and not any(m.get("role") == "tool" for m in agent.messages) for m in agent.messages))

        # 情况 B：拒绝 → 不执行任何工具，模型改为文本回复
        agent2 = Agent(cfg, tmp)
        agent2.plan_mode = True
        events2: list[tuple] = []
        rejected = False

        async def reject(calls):
            nonlocal rejected
            rejected = True
            return False

        async def on_event2(e):
            events2.append(e)

        await agent2.chat("读取 hello.txt", on_event=on_event2, approver=reject)
        check("拒绝触发 plan_rejected", any(e[0] == "plan_rejected" for e in events2))
        check("拒绝时不执行任何工具", not any(e[0] == "tool" for e in events2))
        check("拒绝后模型给出文本回复", any(e[0] == "assistant" for e in events2))
        check("拒绝路径无悬挂 tool_calls", not any(m.get("tool_calls") for m in agent2.messages))

        # 情况 C：非计划模式 + 无 approver → 直接执行（旧行为不变）
        agent3 = Agent(cfg, tmp)
        events3: list[tuple] = []

        async def on_event3(e):
            events3.append(e)

        await agent3.chat("读取 hello.txt", on_event=on_event3)
        check("非计划模式直接执行", any(e[0] == "tool" for e in events3) and not any(e[0] == "plan_ready" for e in events3))
    finally:
        server.shutdown()
        server.server_close()


# ======================================================================
# 测试 8：llm.py 瞬时故障重试 + 退避
# ======================================================================
class FlakyHandler(MockHandler):
    """前两次返回 503，第三次返回正常的工具调用流。"""
    call_count = 0

    def do_POST(self):
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        FlakyHandler.call_count += 1
        if FlakyHandler.call_count <= 2:
            # 瞬时 503
            raw = json.dumps({"error": "service unavailable"}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        super().do_POST()


async def test_retries() -> None:
    print("\n[8] 瞬时故障重试")
    FlakyHandler.call_count = 0
    FlakyHandler.seen_headers = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tmp = Path(tempfile.mkdtemp(prefix="lumi-retry-"))
        (tmp / "hello.txt").write_text("Hello LumiAgent!", encoding="utf-8")
        cfg = Config(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            api_key="sk-mock",
            model="mock-model",
            max_retries=3,
        )
        agent = Agent(cfg, tmp)
        events: list[tuple] = []

        async def on_event(e):
            events.append(e)

        await agent.chat("读取 hello.txt", on_event=on_event)
        check("503 后重试最终成功（至少 3 次尝试）", FlakyHandler.call_count >= 3)
        check("重试后正常执行了工具", any(e[0] == "tool" and e[3] for e in events))

        # 重试耗尽 → 上抛带 transient 标记的 LLMError，不再降级
        FlakyHandler.call_count = 0

        class Always503(FlakyHandler):
            def do_POST(self):
                FlakyHandler.call_count += 1
                raw = json.dumps({"error": "down"}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server2 = ThreadingHTTPServer(("127.0.0.1", 0), Always503)
        t2 = threading.Thread(target=server2.serve_forever, daemon=True)
        t2.start()
        try:
            cfg2 = Config(
                base_url=f"http://127.0.0.1:{server2.server_address[1]}",
                api_key="sk-mock",
                model="mock-model",
                max_retries=2,
            )
            agent2 = Agent(cfg2, tmp)
            errs: list[tuple] = []

            async def on_err(e):
                errs.append(e)

            await agent2.chat("读取 hello.txt", on_event=on_err)
            check("503 穷尽后报错（错误事件）", any(e[0] == "error" for e in errs))
            check("重试次数 = 1 + max_retries", FlakyHandler.call_count == 3)  # 原始1 + 重试2
        finally:
            server2.shutdown()
            server2.server_close()
    finally:
        server.shutdown()
        server.server_close()


# ======================================================================
def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lumi-test-"))
    try:
        test_config(tmp)
        test_tools(tmp)
        test_endpoint()
        test_cache_friendly(tmp)
        test_sessions(tmp)
        asyncio.run(test_agent_e2e())
        asyncio.run(test_agent_plan_mode())
        asyncio.run(test_retries())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 ====")
    if FAILED:
        for f in FAILED:
            print(f"  未通过: {f}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()

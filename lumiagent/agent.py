"""Agent 主循环：消息管理 + Function Calling 工具循环。

与 TUI 解耦：通过 on_event 异步回调把过程事件推给界面（或测试代码）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Config
from .llm import LLMClient, LLMError
from .tools import TOOL_SCHEMAS, execute_tool

MAX_TOOL_ROUNDS = 12

# 事件格式（TUI 与测试共用）：
#   ("user", text)
#   ("assistant_chunk", accumulated_text)     流式增量（可能为 None 回调）
#   ("assistant", final_text)
#   ("tool_round", round_no, tool_count)
#   ("tool", name, raw_arguments, ok, result_text)
#   ("plan_ready", round_no, calls)            计划模式：等待批准；calls=[{name, arguments}]
#   ("plan_approved", round_no)               已批准，开始执行
#   ("plan_rejected", round_no)               已拒绝，回传反馈让模型改方案
#   ("error", message)
Event = tuple[Any, ...]
EventHandler = Callable[[Event], Awaitable[None]] | None

# 计划模式批准器：传入拟执行的工具调用列表，返回是否批准
# calls: [{"name": str, "arguments": str(JSON)}]
PlanApprover = Callable[[list[dict[str, str]]], Awaitable[bool]] | None

# ==== 前缀缓存（prefix cache）不变式 —— 修改以下代码前必读 ====
# 1. 系统提示词在 __init__ 中 format 一次后永不修改，且始终位于 messages[0]；
# 2. tools（TOOL_SCHEMAS）是模块常量，序列化结果逐字节稳定，不得动态重建/重排；
# 3. 消息历史 append-only，绝不回改旧消息；未来做历史裁剪时必须从尾部裁、头部不动。
# 遵守以上三条，DeepSeek/OpenAI 等服务的自动前缀缓存才能每轮命中上一轮的全部前缀。
SYSTEM_PROMPT_TEMPLATE = """你是 LumiAgent，一个运行在用户电脑上的文件助手 AI Agent。

# 工作范围
- 工作根目录：{root}
- 你只能使用提供的工具操作文件；路径默认相对于工作根目录（如 "notes/todo.txt"）。

# 工作方式
1. 不要猜测文件内容或路径：路径不明确时先 list_directory 查看结构，读内容用 read_file。
2. 修改已有文件前先 read_file 读取现状，避免覆盖用户未提及的内容；重要写入后可回读验证。
3. 大文件用 offset/limit 分段读取；写入内容一律为 UTF-8。
4. 互不依赖的多个操作尽量在同一轮中一并调用工具，减少往返轮次。
5. 工具返回失败时如实告知错误原因，并给出可执行的下一步；绝不编造工具结果。

# 回复风格
- 使用与用户相同的语言，简洁直接。
- 完成操作后汇报：做了什么、涉及哪些路径、结果如何。"""


class Agent:
    def __init__(self, config: Config, root: Path):
        self.config = config
        self.root = root
        self.client = LLMClient(config)
        # 计划模式：开启后，每轮工具调用先交给 approver 批准；False 表示无 approver 时也只汇报不执行
        self.plan_mode: bool = False
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(root=root)}
        ]

    def system_prompt(self) -> str:
        """返回当前系统提示词（前缀缓存静态前缀，供会话管理判断头部稳定性）。"""
        return self.messages[0]["content"]

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """用一份已保存的会话历史替换当前历史（会话载入用）。

        要求传入消息的开头与本 Agent 的系统提示词一致（否则前缀缓存会冷启动）；
        不一致时用本 Agent 的系统提示词替换保留头部，保证缓存友好。
        """
        if messages and messages[0].get("content") == self.system_prompt():
            self.messages = list(messages)
        else:
            # 头部不一致：保留当前稳定系统提示词，仅载入其后的对话
            self.messages = [self.messages[0]] + [m for m in messages if m.get("role") != "system"]

    # ------------------------------------------------------------------
    async def chat(
        self,
        user_text: str,
        on_event: EventHandler = None,
        approver: PlanApprover = None,
    ) -> None:
        async def emit(event: Event) -> None:
            if on_event is not None:
                await on_event(event)

        self.messages.append({"role": "user", "content": user_text})
        await emit(("user", user_text))

        for round_idx in range(1, MAX_TOOL_ROUNDS + 1):
            try:
                result = await self.client.chat(
                    self.messages,
                    TOOL_SCHEMAS,
                    on_chunk=lambda text: emit(("assistant_chunk", text)),
                )
            except LLMError as e:
                await emit(("error", f"[LLM] {e}"))
                return
            except Exception as e:  # noqa: BLE001 —— 兜底，不让界面崩掉
                await emit(("error", f"[内部错误] {type(e).__name__}: {e}"))
                return

            if result.tool_calls:
                # 模型常先输出一句解释再发起工具调用，把它展示给用户（仅界面，不入额外历史）
                preamble = (result.content or "").strip()
                if preamble:
                    await emit(("assistant", preamble))
                # 计划模式：先暂停，把拟执行的工具调用交给 approver 批准
                if self.plan_mode and approver is not None:
                    plan_calls = [
                        {"name": tc.name, "arguments": tc.arguments or "{}"}
                        for tc in result.tool_calls
                    ]
                    await emit(("plan_ready", round_idx, plan_calls))
                    approved = await approver(plan_calls)
                    if not approved:
                        await emit(("plan_rejected", round_idx))
                        # 不把含 tool_calls 的 assistant 消息入历史（避免悬挂 tool_calls 没有对应 tool 结果）
                        summary = "; ".join(f"{c['name']}({c['arguments'][:80]})" for c in plan_calls)
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "（计划模式）用户拒绝了如下计划，请根据反馈调整或改为直接回复："
                                    + summary
                                ),
                            }
                        )
                        continue
                    await emit(("plan_approved", round_idx))

                await emit(("tool_round", round_idx, len(result.tool_calls)))
                call_ids = [
                    tc.id or f"call_{round_idx}_{i}" for i, tc in enumerate(result.tool_calls)
                ]
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": result.content or None,
                        "tool_calls": [
                            {
                                "id": call_ids[i],
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": tc.arguments or "{}",
                                },
                            }
                            for i, tc in enumerate(result.tool_calls)
                        ],
                    }
                )
                for tc, call_id in zip(result.tool_calls, call_ids):
                    ok, text = execute_tool(self.root, tc.name, tc.arguments)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": text}
                    )
                    await emit(("tool", tc.name, tc.arguments, ok, text))
                continue

            content = (result.content or "").strip()
            if content:
                self.messages.append({"role": "assistant", "content": content})
                await emit(("assistant", content))
            else:
                await emit(("error", "模型返回了空内容，请重试或检查模型配置。"))
            return

        await emit(("error", f"工具调用轮次超过上限（{MAX_TOOL_ROUNDS} 轮），已停止。"))

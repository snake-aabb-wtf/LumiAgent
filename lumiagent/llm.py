"""OpenAI 兼容 Chat Completions 客户端（纯 HTTP，httpx）。

支持：
- 任意 OpenAI 兼容服务（OpenAI / DeepSeek / Qwen / Ollama / vLLM 等）
- 流式输出（SSE），含流式 tool_calls 增量合并
- 流式失败时自动降级为非流式重试一次
- 模型不支持 tools 时（4xx 且错误含 tool 字样）自动去掉 tools 重试一次
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .config import Config

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=15.0)
BACKOFF_BASE = 0.5  # 指数退避基数（秒）
BACKOFF_CAP = 8.0  # 单次退避上限（秒）

# 视为可重试的瞬时故障状态码
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str = "",
        transient: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.transient = transient  # 是否属已穷尽重试的瞬时故障（用于决定降级策略）


@dataclass
class ToolCall:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None


class LLMClient:
    def __init__(self, config: Config, timeout: httpx.Timeout = DEFAULT_TIMEOUT):
        self._config = config
        self._timeout = timeout

    # ------------------------------------------------------------------
    def _endpoint(self) -> str:
        base = (self._config.base_url or "").strip().rstrip("/")
        if not base:
            raise LLMError("base_url 未配置（使用 /config 或 /set base_url <url>）")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "LumiAgent/0.1",
        }
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _payload(self, messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model or "gpt-4o-mini",
            "messages": messages,
            "stream": bool(self._config.stream),
        }
        if tools:
            payload["tools"] = tools
        if self._config.temperature is not None:
            payload["temperature"] = self._config.temperature
        if self._config.max_tokens is not None:
            payload["max_tokens"] = self._config.max_tokens
        return payload

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatResult:
        """发起一次 chat 请求。on_chunk 会在每个内容增量到达时收到累计文本。

        内部做了三层增强：瞬时故障指数退避重试、流式失败降级非流式、
        模型不支持 tools 时去掉 tools 重试。降级之间互斥不叠加。
        """
        payload = self._payload(messages, tools)
        headers = self._headers()
        try:
            return await self._request(payload, headers, on_chunk)
        except LLMError as e:
            # 降级 1：流式失败（非瞬时网络问题）→ 非流式重试一次
            if payload.get("stream") and e.status_code is None and not e.transient:
                payload2 = dict(payload)
                payload2["stream"] = False
                return await self._request(payload2, headers, None)
            # 降级 2：模型不支持 tools（4xx 且错误含 tool 字样）→ 去掉 tools 重试一次
            if tools and e.status_code in (400, 404) and "tool" in e.body.lower():
                payload3 = dict(payload)
                payload3["tools"] = []
                return await self._request(payload3, headers, on_chunk)
            raise

    async def _request(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], Awaitable[None]] | None,
    ) -> ChatResult:
        """执行请求，对瞬时故障（网络错误 / 超时 / 5xx / 429）指数退避重试。

        非瞬时 LLMError（4xx 等）立即上抛，交给 chat() 决定是否降级。
        """
        max_retries = max(0, int(getattr(self._config, "max_retries", 3)))
        last_err: LLMError | None = None
        for attempt in range(max_retries + 1):
            try:
                if payload.get("stream"):
                    return await self._chat_stream(payload, headers, on_chunk)
                return await self._chat_once(payload, headers)
            except LLMError as e:
                transient = e.status_code in _TRANSIENT_STATUS
                if not transient:
                    raise  # 4xx 等非瞬时错误，交给 chat() 降级
                last_err = e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = LLMError(
                    f"{'请求超时' if isinstance(e, httpx.TimeoutException) else '网络错误'}: {type(e).__name__}: {e}",
                    status_code=None,
                    transient=True,
                )
            if attempt < max_retries:
                await asyncio.sleep(min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP))
        # 穷尽重试：把最后一次瞬时错误标 transient 上抛
        assert last_err is not None
        last_err.transient = True
        raise last_err

    # ------------------------------------------------------------------
    async def _chat_once(self, payload: dict[str, Any], headers: dict[str, str]) -> ChatResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._endpoint(), json=payload, headers=headers)
            if resp.status_code != 200:
                raise self._error_from_response(resp)
            return self._parse_message(resp.json())

    async def _chat_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], Awaitable[None]] | None,
    ) -> ChatResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", self._endpoint(), json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise LLMError(
                        f"API 请求失败 (HTTP {resp.status_code}): {body[:500]}",
                        resp.status_code,
                        body,
                    )
                return await self._read_sse(resp, on_chunk)

    async def _read_sse(
        self,
        resp: httpx.Response,
        on_chunk: Callable[[str], Awaitable[None]] | None,
    ) -> ChatResult:
        content_parts: list[str] = []
        tool_calls: dict[int, ToolCall] = {}
        finish_reason: str | None = None

        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue  # 忽略注释 / keep-alive 等
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue  # 忽略 usage 等无 choices 的块
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
                if on_chunk is not None:
                    await on_chunk("".join(content_parts))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                cur = tool_calls.setdefault(idx, ToolCall(index=idx))
                if tc.get("id"):
                    cur.id += tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    cur.name += fn["name"]
                if fn.get("arguments"):
                    cur.arguments += fn["arguments"]

        calls = sorted(tool_calls.values(), key=lambda c: c.index)
        return ChatResult(
            content="".join(content_parts),
            tool_calls=calls,
            finish_reason=finish_reason,
        )

    # ------------------------------------------------------------------
    def _parse_message(self, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            return ChatResult()
        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tool_calls.append(
                ToolCall(
                    index=i,
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments") or "{}",
                )
            )
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
        )

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> LLMError:
        try:
            body = resp.text
        except Exception:
            body = ""
        hint = ""
        if resp.status_code in (401, 403):
            hint = "（api_key 无效或无权限？）"
        elif resp.status_code == 429:
            hint = "（请求频率受限，请稍后再试）"
        return LLMError(f"API 请求失败 (HTTP {resp.status_code}){hint}: {body[:500]}", resp.status_code, body)

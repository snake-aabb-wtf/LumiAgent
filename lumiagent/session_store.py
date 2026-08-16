"""会话持久化：把对话历史存到 ~/.LumiAgent/sessions/<id>.json。

- 跨进程保存/加载一份 Agent 对话的最小可恢复状态（system + turns）；
- 不写入临时流式字段，保证写入幂等、可被 git 友好地 diff。
- 不包含 api_key 等敏感信息。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path.home() / ".LumiAgent" / "sessions"
SESSION_FILE_RE = re.compile(r"^[0-9a-zA-Z]{8}$")


def ensure_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def _path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def new_session_id() -> str:
    """生成形如 '1ab3f9c2' 的 8 位会话 id。"""
    import secrets

    return secrets.token_hex(4)


def load_all() -> list[dict[str, Any]]:
    """列出全部会话摘要（按更新时间倒序）。"""
    ensure_dir()
    items: list[dict[str, Any]] = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append(
            {
                "id": p.stem,
                "updated": data.get("updated", 0),
                "created": data.get("created", 0),
                "title": data.get("title", "(无标题)"),
                "message_count": max(0, len(data.get("messages", [])) - 1),
            }
        )
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def load(session_id: str) -> dict[str, Any] | None:
    """读取单个会话的完整内容。"""
    p = _path(session_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(
    session_id: str,
    messages: list[dict[str, Any]],
    title: str | None = None,
    created: float | None = None,
) -> dict[str, Any]:
    """覆盖写入指定会话。返回写入的元数据。"""
    ensure_dir()
    created_ts = created if created is not None else time.time()
    record = {
        "id": session_id,
        "created": created_ts,
        "updated": time.time(),
        "title": title or _derive_title(messages),
        "messages": [_clean(m) for m in messages],
    }
    _path(session_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record


def delete(session_id: str) -> bool:
    p = _path(session_id)
    if p.is_file():
        p.unlink()
        return True
    return False


def derive_title_from_message(text: str) -> str:
    """从用户消息生成会话标题。"""
    t = text.strip().replace("\n", " ")
    if not t:
        return "(无标题)"
    return t[:40] + ("…" if len(t) > 40 else "")


def _derive_title(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            return derive_title_from_message(str(m["content"]))
    return "(无标题)"


def _clean(m: dict[str, Any]) -> dict[str, Any]:
    """规范化单条消息，剔除流式临时字段。"""
    out: dict[str, Any] = {}
    for k in ("role", "content", "tool_calls", "tool_call_id"):
        if k in m:
            v = m[k]
            # content=null 与缺失都写为 null，保证可 diff
            out[k] = v if v is not None else None
    return out
"""文件操作工具集：Agent 通过 Function Calling 调用这些工具。

路径约定：
- 默认相对于工作根目录解析（例如 "notes/todo.txt"）；
- 也允许绝对路径；
- 当前版本不做安全策略（不限制路径穿越 / 越权读写），后续版本再加入。

每个工具返回 (ok, text)，其中 text 会原样回传给模型。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

MAX_READ_LINES = 5000                  # 全量读取时最多返回的行数
MAX_READ_BYTES = 20 * 1024 * 1024      # 全量读取时文件大小上限（20MB）
LIST_MAX_ENTRIES = 200                 # list_directory 默认最多列出条数


# ----------------------------------------------------------------------
# 路径解析
# ----------------------------------------------------------------------
def _resolve(root: Path, raw: str | None) -> Path:
    raw = (raw or "").strip()
    if not raw:
        return root
    p = Path(raw)
    if p.is_absolute():
        return p
    # 模型可能给出 "/notes/x.txt" 或 "\\notes\\x.txt" 这类根相对写法，
    # 去掉前导分隔符后按根目录相对解析
    while raw.startswith("/") or raw.startswith("\\"):
        raw = raw[1:]
    return (root / raw).resolve()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.1f} GB"


# ----------------------------------------------------------------------
# 工具实现（签名统一为 handler(root, **args)）
# ----------------------------------------------------------------------
def _looks_binary(fp: Path, sample_size: int = 4096) -> bool:
    """嗅探开头字节：含 NUL 或不可打印比例过高 → 判定为二进制。"""
    try:
        with fp.open("rb") as f:
            chunk = f.read(sample_size)
    except OSError:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    # 去除常见文本空白后，不可打印控制字符占比过高也算二进制
    textish = bytes(c for c in chunk if c not in (9, 10, 13))  # 去掉 \t \n \r
    if not textish:
        return False
    nonprint = sum(1 for c in textish if c < 0x20 or c == 0x7F)
    return nonprint / len(textish) > 0.30


def read_file(root: Path, path: str, offset: int | None = None, limit: int | None = None) -> str:
    """读取文件内容，返回带行号的文本。"""
    fp = _resolve(root, path)
    if not fp.is_file():
        raise FileNotFoundError(f"文件不存在: {fp}")

    # 二进制守卫：避免把图片/压缩包等以 errors=replace 的乱码塞进上下文
    if _looks_binary(fp):
        size = _fmt_size(fp.stat().st_size)
        raise ValueError(
            f"{fp} 疑似二进制文件（{size}），read_file 仅支持文本；"
            f"如需元信息请用 get_file_info"
        )

    if offset is not None:
        start = max(1, int(offset))
        count = int(limit) if limit is not None else 500
        lines: list[str] = []
        with fp.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i < start:
                    continue
                if i >= start + count:
                    break
                lines.append(f"{i:>6} | {line.rstrip(chr(10)).rstrip(chr(13))}")
        if not lines:
            raise ValueError(f"起始行 {start} 超出文件总行数（文件大小 {_fmt_size(fp.stat().st_size)}）")
        return (
            f"文件: {fp}（分段读取 第 {start}~{start + len(lines) - 1} 行）\n"
            + "\n".join(lines)
        )

    size = fp.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(
            f"文件超过 {MAX_READ_BYTES // 1024 // 1024}MB（当前 {_fmt_size(size)}），"
            f"为避免撑爆上下文请用 offset/limit 分段读取"
        )
    data = fp.read_text(encoding="utf-8", errors="replace")
    lines = data.splitlines()
    total = len(lines)
    shown = lines[:MAX_READ_LINES]
    out = [f"{i:>6} | {line}" for i, line in enumerate(shown, start=1)]
    note = ""
    if total > MAX_READ_LINES:
        note = f"\n...（共 {total} 行，已截断，可用 offset={MAX_READ_LINES + 1} 继续读取）"
    return f"文件: {fp}（共 {total} 行）\n" + "\n".join(out) + note


def write_file(root: Path, path: str, content: str) -> str:
    """创建或整体覆盖一个文件（父目录自动创建）。"""
    fp = _resolve(root, path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return f"OK: 已写入 {len(content)} 字符 → {fp}"


def append_file(root: Path, path: str, content: str) -> str:
    """在文件末尾追加内容（文件不存在则创建）。"""
    fp = _resolve(root, path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as f:
        f.write(content)
    return f"OK: 已追加 {len(content)} 字符，文件现大小 {_fmt_size(fp.stat().st_size)} → {fp}"


def delete_file(root: Path, path: str) -> str:
    """删除文件（不支持删除目录）。"""
    fp = _resolve(root, path)
    if not fp.exists():
        raise FileNotFoundError(f"不存在: {fp}")
    if fp.is_dir():
        raise IsADirectoryError(f"{fp} 是目录，本工具只能删除文件")
    fp.unlink()
    return f"OK: 已删除 {fp}"


def list_directory(
    root: Path,
    path: str = ".",
    recursive: bool = False,
    max_entries: int = LIST_MAX_ENTRIES,
) -> str:
    """列出目录内容（文件大小 + 名称；目录带 [DIR] 标记）。"""
    d = _resolve(root, path)
    if not d.is_dir():
        raise NotADirectoryError(f"不是目录: {d}")
    if recursive:
        entries = sorted(d.rglob("*"), key=lambda p: str(p).lower())
    else:
        entries = sorted(d.iterdir(), key=lambda p: str(p).lower())
    total = len(entries)
    shown = entries[: max(1, int(max_entries))]
    lines: list[str] = []
    for p in shown:
        rel = p.relative_to(d) if recursive else p.name
        try:
            if p.is_dir():
                lines.append(f"[DIR]    {rel}/")
            else:
                lines.append(f"{_fmt_size(p.stat().st_size):>10}  {rel}")
        except OSError:
            lines.append(f"[?]      {rel}")
    head = f"目录: {d}（共 {total} 项）"
    if total > len(shown):
        head += f"，仅显示前 {len(shown)} 项"
    return "\n".join([head] + lines)


def create_directory(root: Path, path: str, recursive: bool = True) -> str:
    """新建目录（默认递归创建父目录）。"""
    fp = _resolve(root, path)
    fp.mkdir(parents=recursive, exist_ok=True)
    return f"OK: 目录已就绪 {fp}"


def rename_file(root: Path, source: str, destination: str) -> str:
    """重命名或移动文件/目录。"""
    src = _resolve(root, source)
    dst = _resolve(root, destination)
    if not src.exists():
        raise FileNotFoundError(f"不存在: {src}")
    if dst.exists():
        raise FileExistsError(f"目标已存在: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"OK: {src} → {dst}"


def get_file_info(root: Path, path: str) -> str:
    """查看文件/目录的大小、修改时间等元信息。"""
    fp = _resolve(root, path)
    if not fp.exists():
        raise FileNotFoundError(f"不存在: {fp}")
    st = fp.stat()
    kind = "目录" if fp.is_dir() else "文件"
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"路径: {fp}\n"
        f"类型: {kind}\n"
        f"大小: {_fmt_size(st.st_size)}\n"
        f"修改时间: {mtime}"
    )


# ----------------------------------------------------------------------
# Function Calling 的 JSON Schema 定义与执行器
#
# ⚠️ 前缀缓存不变式：TOOL_SCHEMAS 是模块常量，每次请求原样下发且逐字节稳定。
# 不要按请求动态重建/重排/增删字段——任何变动都会让缓存前缀全部失效。
# ----------------------------------------------------------------------
def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "read_file",
        "读取文件内容，返回带行号的文本。可用于查看代码、配置、日志等。大文件可用 offset/limit 分段读取。",
        {
            "path": {"type": "string", "description": "文件路径，相对于工作根目录"},
            "offset": {"type": "integer", "description": "起始行号（从 1 开始），可选"},
            "limit": {"type": "integer", "description": "最多读取的行数，可选"},
        },
        ["path"],
    ),
    _schema(
        "write_file",
        "创建新文件或整体覆盖已有文件的内容（父目录不存在时自动创建）。",
        {
            "path": {"type": "string", "description": "文件路径，相对于工作根目录"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        ["path", "content"],
    ),
    _schema(
        "append_file",
        "在文件末尾追加内容（文件不存在时自动创建）。适合日志、增量记录等场景。",
        {
            "path": {"type": "string", "description": "文件路径，相对于工作根目录"},
            "content": {"type": "string", "description": "要追加的内容"},
        },
        ["path", "content"],
    ),
    _schema(
        "delete_file",
        "删除指定文件（注意：只能删除文件，不能删除目录）。",
        {
            "path": {"type": "string", "description": "要删除的文件路径，相对于工作根目录"},
        },
        ["path"],
    ),
    _schema(
        "list_directory",
        "列出目录下的文件与子目录（含大小）。可递归列出子目录。",
        {
            "path": {"type": "string", "description": "目录路径，相对于工作根目录；默认当前根目录", "default": "."},
            "recursive": {"type": "boolean", "description": "是否递归列出所有子目录", "default": False},
            "max_entries": {"type": "integer", "description": "最多列出多少项", "default": 200},
        },
    ),
    _schema(
        "create_directory",
        "新建目录（默认递归创建父目录）。",
        {
            "path": {"type": "string", "description": "目录路径，相对于工作根目录"},
            "recursive": {"type": "boolean", "description": "是否递归创建父目录", "default": True},
        },
        ["path"],
    ),
    _schema(
        "rename_file",
        "重命名文件/目录，或移动到新位置（跨目录移动也可以）。",
        {
            "source": {"type": "string", "description": "原路径，相对于工作根目录"},
            "destination": {"type": "string", "description": "新路径，相对于工作根目录"},
        },
        ["source", "destination"],
    ),
    _schema(
        "get_file_info",
        "查看文件或目录的元信息：大小、类型、修改时间。",
        {
            "path": {"type": "string", "description": "文件或目录路径，相对于工作根目录"},
        },
        ["path"],
    ),
]

_TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "read_file": read_file,
    "write_file": write_file,
    "append_file": append_file,
    "delete_file": delete_file,
    "list_directory": list_directory,
    "create_directory": create_directory,
    "rename_file": rename_file,
    "get_file_info": get_file_info,
}


def execute_tool(root: Path, name: str, raw_arguments: str) -> tuple[bool, str]:
    """执行一次工具调用。返回 (是否成功, 结果文本)。结果文本会原样回传给模型。"""
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return False, f"未知工具: {name}（可用工具: {', '.join(_TOOL_HANDLERS)}）"

    try:
        args = json.loads(raw_arguments) if (raw_arguments or "").strip() else {}
        if not isinstance(args, dict):
            return False, f"工具参数必须是 JSON 对象，收到: {raw_arguments[:200]}"
    except json.JSONDecodeError as e:
        return False, f"工具参数 JSON 解析失败: {e}；原始参数: {raw_arguments[:200]}"

    try:
        result = handler(root, **args)
        return True, str(result)
    except TypeError as e:
        return False, f"参数错误: {e}"
    except Exception as e:  # noqa: BLE001 —— 错误要回传给模型，不能吞掉
        return False, f"{type(e).__name__}: {e}"

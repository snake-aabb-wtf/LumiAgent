"""LumiAgent 配置管理。

配置文件 config.json 存放在 Agent 根目录，TUI 内可通过
/config 面板或 /set 命令修改并热生效，也可手动编辑。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

CONFIG_FIELDS = ("base_url", "api_key", "model", "stream", "temperature", "max_tokens", "max_retries")


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    max_retries: int = 3  # 瞬时故障（连接/超时/5xx/429）的指数退避重试次数

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cls()
            known = {f.name: f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known})
        except FileNotFoundError:
            return cls()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # 配置文件损坏时回退到默认值，不让程序崩掉
            return cls()

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # 运行时修改（/set 与 /config 面板共用）
    # ------------------------------------------------------------------
    def set_field(self, field: str, value: Any) -> tuple[bool, str]:
        f = str(field).strip().lower()
        if f == "base_url":
            v = str(value).strip()
            if not v.startswith(("http://", "https://")):
                return False, "base_url 必须以 http:// 或 https:// 开头"
            self.base_url = v
        elif f == "api_key":
            self.api_key = str(value).strip()
        elif f == "model":
            v = str(value).strip()
            if not v:
                return False, "model 不能为空"
            self.model = v
        elif f == "stream":
            v = str(value).strip().lower()
            if v in ("true", "1", "yes", "on"):
                self.stream = True
            elif v in ("false", "0", "no", "off"):
                self.stream = False
            else:
                return False, "stream 需要为 true 或 false"
        elif f == "temperature":
            v = str(value).strip()
            if v.lower() in ("none", "null", ""):
                self.temperature = None
            else:
                try:
                    t = float(v)
                except ValueError:
                    return False, "temperature 需要是数字（0~2）"
                if not (0.0 <= t <= 2.0):
                    return False, "temperature 范围是 0~2"
                self.temperature = t
        elif f == "max_tokens":
            v = str(value).strip()
            if v.lower() in ("none", "null", ""):
                self.max_tokens = None
            else:
                try:
                    n = int(v)
                except ValueError:
                    return False, "max_tokens 需要是正整数"
                if n <= 0:
                    return False, "max_tokens 需要 > 0"
                self.max_tokens = n
        elif f == "max_retries":
            v = str(value).strip().lower()
            if v in ("none", "null", "0"):
                self.max_retries = 0
            else:
                try:
                    n = int(v)
                except ValueError:
                    return False, "max_retries 需要是非负整数"
                if n < 0:
                    return False, "max_retries 需要 >= 0"
                if n > 10:
                    return False, "max_retries 上限 10（避免长时间挂起）"
                self.max_retries = n
        else:
            return (
                False,
                f"未知配置项: {field}（可用: {', '.join(CONFIG_FIELDS)}）",
            )
        return True, f"已设置 {f} = {getattr(self, f)}"

    def api_ready(self) -> tuple[bool, str]:
        if not self.base_url.strip():
            return False, "base_url 未配置，请使用 /config 或 /set base_url <url>"
        if not self.model.strip():
            return False, "model 未配置，请使用 /config 或 /set model <name>"
        if not self.api_key.strip():
            # 允许无 key（如本地 Ollama），仅提示
            return True, "未配置 api_key（本地服务可跳过，远程服务需要）"
        return True, ""

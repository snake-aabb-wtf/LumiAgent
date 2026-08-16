"""定位 LumiAgent 的“工作根目录”。

- 打包为 exe（PyInstaller 等，sys.frozen 为 True）运行时：取 exe 所在目录；
- 源码运行时：取项目根目录（lumiagent 包的上一级）。

所有文件工具的路径都默认相对于该根目录解析。
"""
from __future__ import annotations

import sys
from pathlib import Path


def get_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent

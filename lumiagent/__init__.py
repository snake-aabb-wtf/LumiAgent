"""LumiAgent —— 运行在本机的文件助手 AI Agent。

通过 OpenAI 兼容的 Chat Completions API + Function Calling，
对 Agent 根目录（源码运行=项目根目录；打包 exe 后=exe 所在目录）下的
文件进行读取、创建、写入、追加、删除、重命名、列目录等操作。
"""

__version__ = "0.1.0"

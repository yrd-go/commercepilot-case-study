# -*- coding: utf-8 -*-
"""后端 Web 层：Starlette 应用 + 纯函数 API。

为什么不继续用 Streamlit
------------------------
Streamlit 的长处是"几行代码出个能点的页面"，短处恰好是这个项目现在最需要的：
版面基本由它决定（间距、字体、控件外观都改不动），任何定制都要绕一大圈，
最后得到的是一个"内部工具"的观感。而这个项目要拿去当作品集，**第一眼观感就是门槛**。

为什么选 Starlette 而不是 FastAPI
---------------------------------
底座环境里已经装了 `starlette` + `uvicorn` + `orjson`，**没有 fastapi**。
Starlette 就是 FastAPI 底下的那层 ASGI 框架，满足这里的全部需要（路由、
静态文件、JSON 响应），而且**一个新依赖都不用装** —— 不用碰共用的底座虚拟环境，
这在"换台机器也要能跑"这件事上是决定性的。
"""
from __future__ import annotations

__all__ = ["api", "jobs", "server"]

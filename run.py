# -*- coding: utf-8 -*-
"""统一入口：把 src/ 加进 sys.path 再转交 CLI。

为什么需要这个文件
------------------
不装包（pip install -e .）也能直接跑：`python run.py weekly`。
评审者拿到源码后按 README 安装依赖，再运行 `python run.py` 即可；入口负责把
`src/` 加入导入路径，减少换机器后的环境配置问题。

★ 还有一步：**解释器守卫**。机器上默认的 `python` 可能是 3.14（没装本项目依赖），
而依赖在底座那个 3.12 的虚拟环境里。直接敲 `python run.py` 时，
`interp.ensure()` 会自动换成装了依赖的解释器重新执行，
而不是甩一堆 `ModuleNotFoundError`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from saviland.interp import ensure as ensure_interpreter  # noqa: E402

if __name__ == "__main__":
    ensure_interpreter("run.py")

from saviland.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

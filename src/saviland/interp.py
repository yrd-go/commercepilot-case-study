# -*- coding: utf-8 -*-
"""解释器守卫：当前 python 缺依赖时，自动换到"装了依赖的那个"重新执行。

为什么需要这个
--------------
本项目的依赖（starlette / uvicorn / chromadb / langchain / Pillow / pandas）
装在底座那个 **Python 3.12 虚拟环境**里，而机器上默认的 `python` 可能是
Python 3.14（没有这些包）。

于是最常见的翻车是这样：

    PS> python tests\\run_all.py
    ...
    ModuleNotFoundError: No module named 'starlette'

报错本身没错，但它把"你用错了解释器"这个**真正原因**藏在了 4 个模块错误里。
文档里写"请用 <底座venv>\\python.exe"是没用的 —— 人会直接敲 `python`。

所以入口层主动做三件事：
1. 检查关键依赖是否齐全（`find_spec`，不 import，避免副作用）；
2. 不齐就去找一个齐全的解释器（环境变量 → 底座 venv → 项目内 .venv）；
3. 找到就用它 `execv` 重新执行自己，并打印一行说明；找不到就给出**可照抄的命令**。

设计约束：
- 不 import 任何业务模块（要有依赖才能跑，而这一步就是来解决依赖的）；
- 重入保护（`SAVILAND_NO_REEXEC`），绝不允许自己 exec 自己形成死循环。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# 与项目其它模块一致：Windows 控制台默认 GBK，不显式改会在打印中文时崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 一个"能完整跑起来"的解释器至少要有的包。
# 名字用 importlib 的模块名（不是 pip 名），例如 chromadb 是 chromadb，
# langchain-community 是 langchain_community。
REQUIRED = (
    "starlette",          # Web 层
    "uvicorn",            # Web 层
    "chromadb",           # 向量库（RAG 底座）
    "langchain_community",  # RAG 底座
    "PIL",                # 主图渲染
    "pandas",             # 数据分析
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# 候选解释器：按"最可能装好依赖"的顺序
CANDIDATES = (
    str(Path.home() / "my-agent" / ".venv-rag" / "Scripts" / "python.exe"),
)


def _site_dirs(python_exe: Path) -> list[Path]:
    """列出某个解释器可能的 site-packages 目录（Windows / Unix / venv / 系统安装）。

    为什么不去问解释器自己（`-c "import sys; print(site.getsitepackages())"`）：
    那要起子进程、抓输出。而在受限环境（沙箱、CI、某些企业策略）里
    **子进程的输出捕获可能直接失败**，于是"探测"永远返回"缺依赖"，
    守卫反而把唯一正确的解释器判成不可用。目录检查没有这些问题。
    """
    parents = [python_exe.parent, python_exe.parent.parent]
    dirs: list[Path] = []
    for base in parents:
        dirs.append(base / "Lib" / "site-packages")          # Windows
        dirs.append(base / "lib" / "site-packages")          # Unix venv
        if (base / "lib").is_dir():
            dirs.extend(sorted((base / "lib").glob("python3*/site-packages")))
    return [d for d in dirs if d.is_dir()]


def _has_module(site_dirs: list[Path], module: str) -> bool:
    for d in site_dirs:
        if (d / module).is_dir() or (d / f"{module}.py").is_file():
            return True
        # 有些包只以 dist-info 形式出现直接可见（不常见，但兜一层）
        if list(d.glob(f"{module.replace('_', '-')}-*.dist-info")):
            return True
    return False


def missing_modules(python: str | None = None) -> list[str]:
    """返回缺哪些模块。`python=None` 表示检查**当前**解释器。

    当前解释器用 `importlib.util.find_spec`（精确、无副作用）；
    其它解释器用 site-packages 目录检查（不需要起子进程）。
    """
    if python is None:
        return [m for m in REQUIRED if importlib.util.find_spec(m) is None]

    site = _site_dirs(Path(python))
    if not site:
        return list(REQUIRED)
    return [m for m in REQUIRED if not _has_module(site, m)]


def find_working_python() -> str | None:
    """找一个依赖齐全的解释器；没有就返回 None。"""
    env = os.getenv("SAVILAND_PYTHON", "").strip()
    pool = [env] if env else []
    pool.extend(CANDIDATES)
    pool.extend([
        str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"),
        str(REPO_ROOT / ".venv" / "bin" / "python"),
    ])

    seen: set[str] = set()
    for cand in pool:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if not Path(cand).is_file():
            continue
        if not missing_modules(cand):
            return cand
    return None


def _reexec(python: str) -> None:
    argv = [python, *sys.argv]
    print(f"[解释器] 当前 {sys.executable} 缺少项目依赖，改用：{python}")
    print(f"[解释器] 等价命令：{python} {' '.join(sys.argv)}")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        raise SystemExit(subprocess.call(argv))
    except OSError as e:
        print(f"[解释器] 切换失败（{e}），请手动用上面的命令运行。")
        raise SystemExit(2)


def ensure(program: str = "本项目") -> None:
    """入口层第一行调用它。

    依赖齐全 → 什么都不做（正常路径零成本）；
    不齐全 → 换解释器重跑，或者打印可照抄的解决办法后退出。
    """
    if os.getenv("SAVILAND_NO_REEXEC") == "1":
        return

    missing = missing_modules()
    if not missing:
        return

    print(f"[解释器] 当前解释器缺少 {len(missing)} 个依赖：{', '.join(missing)}")
    print(f"[解释器] 当前：{sys.executable}")

    python = find_working_python()
    if python:
        _reexec(python)
        return

    print(f"\n[解释器] 没找到装了这些依赖的 Python，{program}无法启动。\n")
    print("  解决办法：用底座虚拟环境的解释器运行 ——")
    print(f"    {CANDIDATES[0]} {program if program != '本项目' else 'run.py'} ...")
    print("\n  或者设置环境变量后重试：")
    print("    set SAVILAND_PYTHON=<你的 python.exe 路径>")
    print("\n  想确认缺什么、装在哪，可以运行：")
    print("    python -c \"import sys; print(sys.executable)\"")
    raise SystemExit(2)

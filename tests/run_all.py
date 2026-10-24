# -*- coding: utf-8 -*-
"""测试入口：跑全部用例并打印人可读的汇总。

    <底座venv>\\Scripts\\python.exe tests\\run_all.py
    <底座venv>\\Scripts\\python.exe tests\\run_all.py -v      # 显示每个用例

★ 直接 `python tests\\run_all.py` 也可以：如果当前解释器缺依赖（比如默认 python
是 3.14、而依赖装在 3.12 的底座 venv 里），入口会**自动换解释器重跑**，
而不是甩出几个 `ModuleNotFoundError`。

为什么不用 pytest：少一个依赖，少一个"在他机器上装不上"的理由。
unittest 是标准库，永远可用。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from saviland.interp import ensure as ensure_interpreter  # noqa: E402

ensure_interpreter("tests/run_all.py")


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 1
    loader = unittest.TestLoader()
    suite = loader.discover(str(Path(__file__).resolve().parent), pattern="test_*.py")

    print("=" * 74)
    print("  Saviland 电商 AI 系统 —— 测试套件")
    print(f"  解释器：{sys.executable}")
    print(f"  用例总数：{suite.countTestCases()}")
    print("=" * 74)

    t0 = time.time()
    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=False)
    result = runner.run(suite)
    elapsed = time.time() - t0

    print()
    print("=" * 74)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"  用例 {total} 个　通过 {total - failed}　失败 {failed}　"
          f"跳过 {len(result.skipped)}　耗时 {elapsed:.1f} 秒")
    if failed:
        print("  失败清单：")
        for case, tb in result.failures + result.errors:
            print(f"    ✗ {case}")
            print("      " + tb.strip().splitlines()[-1][:150])
    else:
        print("  ✅ 全部通过")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

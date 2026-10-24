# -*- coding: utf-8 -*-
"""Saviland 电商 AI 系统。

一个把「电商后台」和「AI 内容/数据链路」接在一起的演示系统：
    - 电商后台：商品 / 库存 / 订单 / 广告投放 的数据与业务规则
    - 链路 1：主图与卖点文案批量生成（RAG 检索品牌知识 → LLM 生成 → Pillow 合成主图）
    - 链路 2：广告数据周报自动生成（指标计算 → 异常诊断 → LLM 归因 → 图表 + 报告）

RAG/Agent 底座复用自 my-agent（智谱 embedding-3 + Chroma 混合检索 + 查询改写）。
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]

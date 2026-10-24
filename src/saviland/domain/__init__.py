# -*- coding: utf-8 -*-
"""业务域层：商品 / 库存 / 订单 / 广告。每个模块只依赖 db.py，方便单独测试。"""
from __future__ import annotations

__all__ = ["catalog", "inventory", "orders", "ads"]

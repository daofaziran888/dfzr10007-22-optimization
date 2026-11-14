"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import SEED_ROWS


def clone_seed_tables() -> dict[str, list[dict[str, Any]]]:
    """示例数据里有嵌套的字段清单快照，复制时必须深拷贝，不能共享子列表。"""
    return {
        name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
    }


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = clone_seed_tables()
        # 内存仓库被多个请求线程共享；字段清单的“校验-读取版本-写入”需要在同一把锁里完成。
        self.lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def reset(self) -> None:
        """重新载入示例数据；测试或演示需要回到初始状态时使用。"""
        with self.lock:
            self._tables = clone_seed_tables()

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

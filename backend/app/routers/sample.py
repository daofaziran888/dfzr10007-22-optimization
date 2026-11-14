"""数据采样管理接口：维护采样配置，覆盖启用采样、暂停采样、关闭采样等动作。

采样侧只认目录条目的“可查看”状态：
- 列表默认回显引用到的数据集编号、当前数据等级和可用字段；
- 引用已下线数据集时字段为空，配置被标记为引用失效；
- 输出表核对单独提供接口，方便目录刷新前后对照。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.catalog_fields import LEVELS
from app.services.sample import SampleService, sample_columns

router = APIRouter(prefix="/api/sample", tags=["数据采样管理"])

service = SampleService()

LIST_FIELDS = sample_columns()
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


def _level_param(value: str | None) -> str | None:
    if value is None:
        return None
    upper = value.strip().upper()
    if not upper:
        return None
    if upper not in LEVELS:
        raise HTTPException(status_code=400, detail=f"数据等级「{value}」不在 {'、'.join(LEVELS)} 里")
    return upper


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号、数据源名称、采样算法检索"),
    field: str | None = Query(default=None, description="限定检索字段：配置编号、数据源名称、采样算法"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    reference_status: str | None = Query(default=None, description="引用有效、引用已下线、引用不存在"),
    level: str | None = Query(default=None, description="按引用数据集的数据等级过滤：L1~L4"),
    level_min: str | None = Query(default=None, description="数据等级下限"),
    data_level: str | None = Query(default=None, description="同 level，兼容英文参数名"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号、状态、引用状态与数据等级过滤采样配置列表。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"状态「{status}」不在 {'、'.join(STATUSES)} 里")
    # 界面按字段检索时只在该字段内匹配；未知字段名直接忽略，不静默改成全文检索。
    if field == "数据源名称":
        data_source, algorithm, keyword = keyword, None, None
    elif field == "采样算法":
        data_source, algorithm, keyword = None, keyword, None
    else:
        data_source, algorithm = None, None
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        data_source=data_source,
        algorithm=algorithm,
        reference_status=reference_status,
        level=_level_param(level),
        level_min=_level_param(level_min),
        data_level=_level_param(data_level),
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/references", response_model=dict)
def list_references() -> dict[str, Any]:
    """样本输出表引用核对清单：目录侧刷新后直接对照字段、等级与引用状态。"""
    items = service.references()
    blocked = [item for item in items if not item.get("字段可用")]
    return {"module": "sample", "total": len(items), "blocked": len(blocked), "items": items}


@router.get("/references/check/{entry_id}", response_model=dict)
def check_reference(entry_id: int) -> dict[str, Any]:
    """单条采样配置的引用核对：字段、分层字段、输出表名逐项给结论。"""
    result, error = service.reference_check(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=error)
    return result


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据采样管理清单：字段均取引用数据集当前可查看的版本。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sample", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采样配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采样配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采样配置，引用不合法或字段对不上时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message=f"采样配置未登记：{errors[0]}", errors=errors)
    return ActionResult(ok=True, message="采样配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行启用采样、暂停采样、关闭采样；引用已下线数据集时不允许启用。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

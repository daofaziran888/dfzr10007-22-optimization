"""数据目录检索接口：维护目录条目，覆盖发布条目、下线条目、归档条目等动作。

增量更新相关端点：
- PUT  /api/catalog/{entry_id}/fields      单条字段清单增量更新（字段校验 + 版本冲突）
- POST /api/catalog/{entry_id}/fields/preview  只算不写，用于展示重算后的等级
- GET  /api/catalog/{entry_id}/snapshots   历史字段清单快照（按当时等级保留）
- POST /api/catalog/fields/batch           批量字段更新，只重算受影响的条目
- POST /api/catalog/refresh                按条件重算并重新定位受影响记录
- POST /api/catalog/refresh-all            巡检式全量重算（不新增未变化条目版本）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.catalog import CatalogService, catalog_list_fields
from app.services.catalog_fields import ALL_STATUSES, LEVELS

router = APIRouter(prefix="/api/catalog", tags=["数据目录检索"])

service = CatalogService()

LIST_FIELDS = catalog_list_fields()
STATUSES = list(ALL_STATUSES)


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
    keyword: str | None = Query(default=None, description="按条目编号、数据集名称、业务域检索"),
    keyword_field: str | None = Query(default=None, description="限定检索字段：条目编号、数据集名称、业务域"),
    status: str | None = Query(default=None, description="草稿、已发布、已下线、已归档"),
    business_domain: str | None = Query(default=None, description="按业务域过滤"),
    level: str | None = Query(default=None, description="数据等级精确过滤：L1、L2、L3、L4"),
    level_min: str | None = Query(default=None, description="数据等级下限：L2 表示 L2 及以上"),
    include_hidden: bool = Query(default=False, description="是否包含已下线/已归档条目"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """默认只返回可查看的数据目录清单；已下线条目立即移出，需 include_hidden 才可见。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"状态「{status}」不在 {'、'.join(STATUSES)} 里")
    items, total = service.list_entries(
        keyword=keyword,
        keyword_field=keyword_field,
        status=status,
        business_domain=business_domain,
        level=_level_param(level),
        level_min=_level_param(level_min),
        include_hidden=include_hidden,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/viewable", response_model=dict)
def viewable_entries() -> dict[str, Any]:
    """可查看清单（不分页）：目录侧刷新后用于核对采样侧还能引用哪些字段。"""
    items = service.visible_entries()
    return {"module": "catalog", "total": len(items), "items": items}


@router.get("/hidden", response_model=dict)
def hidden_entries() -> dict[str, Any]:
    """已下线/已归档清单：仅用于追溯，不在可查看清单中出现。"""
    items = service.hidden_entries()
    return {"module": "catalog", "total": len(items), "items": items}


@router.post("/refresh", response_model=ActionResult)
def refresh_entries(payload: EntryPayload) -> ActionResult:
    """按增量更新重算并定位受影响记录：字段变了的条目重算，其余不动。

    payload.values 支持 entry_id / 条目编号 / 业务域 / 字段变更 / recalculate_all。
    """
    affected, errors = service.refresh(payload.values)
    if errors:
        return ActionResult(ok=False, message="目录增量刷新未执行", errors=errors)
    return ActionResult(ok=True, message=f"已重算 {len(affected)} 条受字段变化影响的目录记录", affected=affected)


@router.post("/refresh-all", response_model=ActionResult)
def refresh_all(payload: EntryPayload) -> ActionResult:
    """巡检式全量重算：保留每个条目的历史等级快照，不重复生成字段版本。"""
    values = dict(payload.values)
    values["recalculate_all"] = True
    affected, errors = service.refresh(values)
    if errors:
        return ActionResult(ok=False, message="目录全量刷新未执行", errors=errors)
    return ActionResult(ok=True, message="目录全量巡检完成，未变化条目保持原版本", affected=affected)


@router.post("/fields/batch", response_model=ActionResult)
def batch_update_fields(payload: EntryPayload) -> ActionResult:
    """批量字段清单更新：逐条校验，只有字段变化的条目才升版本、重算等级。"""
    raw = payload.values.get("updates")
    if not isinstance(raw, list) or not raw:
        return ActionResult(ok=False, message="请提供 updates 列表", errors=["updates 不能为空"])
    results = service.batch_update_fields(raw)
    invalid = [item for item in results if item["outcome"] == "invalid"]
    conflicts = [item for item in results if item["outcome"] == "conflict"]
    affected = [
        item["entry"] for item in results
        if item["outcome"] == "affected" and "entry" in item
    ]
    if invalid or conflicts:
        return ActionResult(
            ok=False,
            message=f"批量字段更新部分未落定：{len(invalid)} 条校验未通过，{len(conflicts)} 条版本冲突",
            errors=[error for item in invalid for error in item["errors"]],
            conflict=conflicts[0]["conflict"] if conflicts else None,
            affected=affected,
        )
    return ActionResult(ok=True, message=f"已重算 {len(affected)} 条目录记录", affected=affected)


@router.get("/export")
def export_entries(
    include_hidden: bool = Query(default=False, description="是否包含已下线/已归档条目"),
) -> dict[str, Any]:
    """导出数据目录检索清单：默认只导出可查看条目。"""
    items, total = service.list_entries(
        include_hidden=include_hidden, page=1, size=10000
    )
    return {
        "module": "catalog",
        "total": total,
        "include_hidden": include_hidden,
        "items": items,
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条目录条目明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"目录条目 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条目录条目，缺字段或字段清单不合法时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message=f"目录条目未登记：{errors[0]}", errors=errors)
    return ActionResult(ok=True, message="目录条目已登记", entry=entry)


@router.put("/{entry_id}/fields", response_model=ActionResult)
def update_fields(entry_id: int, payload: EntryPayload) -> ActionResult:
    """字段清单增量更新：先过字段校验，版本一致才落库；冲突时返回已落定标记。"""
    entry, errors, conflict = service.update_fields(entry_id, payload.values)
    if conflict is not None:
        return ActionResult(ok=False, message=str(conflict.get("原因")), conflict=conflict)
    if errors:
        return ActionResult(ok=False, message=f"字段清单未通过校验：{errors[0]}", errors=errors)
    return ActionResult(ok=True, message="字段清单已更新，数据等级已按受影响条目重算", entry=entry)


@router.post("/{entry_id}/fields/preview", response_model=ActionResult)
def preview_fields(entry_id: int, payload: EntryPayload) -> ActionResult:
    """字段清单试算：返回重算后的数据等级与快照，不产生新版本。"""
    entry, errors = service.preview_fields(entry_id, payload.values)
    if errors:
        return ActionResult(ok=False, message=f"字段清单未通过校验：{errors[0]}", errors=errors)
    return ActionResult(ok=True, message="字段清单试算结果", entry=entry)


@router.get("/{entry_id}/snapshots", response_model=dict)
def list_snapshots(entry_id: int) -> dict[str, Any]:
    """历史字段清单快照：每份快照保留写入当时的数据等级。"""
    snapshots, error = service.snapshots(entry_id)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return {"entry_id": entry_id, "total": len(snapshots), "items": snapshots}


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行发布条目、下线条目、归档条目；下线会立即把条目移出可查看清单。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

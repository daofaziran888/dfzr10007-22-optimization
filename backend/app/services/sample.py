"""数据采样管理业务规则：状态流转、字段校验与筛选口径都收在这里。

采样配置只允许引用“可查看”的目录条目：
- 引用已下线/已归档数据集时，立即拿不到字段，配置标记为引用失效；
- 引用有效时，字段一律取目录条目当前版本的字段清单和数据等级；
- 输出表名做引用核对，重名或空表名在列表和核对接口里都会提示。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.catalog_fields import (
    VIEWABLE_STATUSES,
    level_name,
    level_rank,
    normalize_fields,
)

MODULE = "sample"
REQUIRED_FIELDS = ["配置编号", "数据源名称", "采样算法"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用采样": "草稿", "暂停采样": "已启用", "关闭采样": "已暂停"}
NEGATIVE_ACTIONS = []

CATALOG_MODULE = "catalog"
CATALOG_CODE_FIELDS = ("数据集编号", "条目编号", "数据集编码")


def catalog_rows() -> list[dict[str, Any]]:
    from app.services.catalog_fields import ensure_catalog_state

    return ensure_catalog_state()


def resolve_catalog(code: str | None) -> dict[str, Any] | None:
    text = str(code or "").strip()
    if not text:
        return None
    for row in catalog_rows():
        if str(row.get("条目编号") or "") == text:
            return row
    return None


def is_viewable(row: dict[str, Any] | None) -> bool:
    return row is not None and str(row.get("status") or "") in VIEWABLE_STATUSES


def reference_code(values: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    """引用编号优先取显式字段，其次复用已落库的引用，最后按数据源名称推断。"""
    for field in CATALOG_CODE_FIELDS:
        text = str(values.get(field) or "").strip()
        if text:
            return text
    if row is not None:
        for field in CATALOG_CODE_FIELDS:
            text = str(row.get(field) or "").strip()
            if text:
                return text
    source = str(values.get("数据源名称") or (row or {}).get("数据源名称") or "").strip()
    if source and resolve_catalog(source) is not None:
        return source
    return ""


def sample_field_view(row: dict[str, Any]) -> dict[str, Any]:
    """采样侧看到的字段视图：只回可查看数据集的当前字段。"""
    code = reference_code({}, row)
    catalog_row = resolve_catalog(code)
    viewable = is_viewable(catalog_row)
    # 已下线数据集不返回任何字段，避免采样拿到过期字段继续跑。
    fields = list(catalog_row.get("字段列表") or []) if viewable else []
    return {
        "数据集编号": code,
        "引用状态": "引用有效" if viewable else ("引用已下线" if catalog_row else "引用不存在"),
        "字段可用": viewable,
        "数据集状态": (catalog_row or {}).get("status"),
        "数据等级": (catalog_row or {}).get("数据等级") if viewable else None,
        "数据等级名称": level_name((catalog_row or {}).get("数据等级")) if viewable else "",
        "更新频率": (catalog_row or {}).get("更新频率") if viewable else None,
        "字段清单": list(fields),
    }


def output_table_issues() -> dict[int, list[str]]:
    """样本输出表引用核对：空表名、跨配置重名、引用失效都给出可读说明。"""
    seen: dict[str, list[int]] = {}
    for row in store.rows(MODULE):
        table = str(row.get("输出表名") or "").strip()
        if table:
            seen.setdefault(table, []).append(int(row.get("id") or 0))
    issues: dict[int, list[str]] = {}
    for row in store.rows(MODULE):
        row_id = int(row.get("id") or 0)
        messages: list[str] = []
        table = str(row.get("输出表名") or "").strip()
        if not table:
            messages.append("未填写样本输出表名")
        elif len(seen.get(table, [])) > 1:
            others = [other for other in seen[table] if other != row_id]
            messages.append(f"输出表名 {table} 与其他配置（{'、'.join(map(str, others))}）重名")
        view = sample_field_view(row)
        if not view["字段可用"]:
            messages.append(f"引用的数据集 {view['数据集编号'] or '—'} {view['引用状态']}，字段不可用")
        if messages:
            issues[row_id] = messages
    return issues


def decorate_sample_row(row: dict[str, Any], issues: dict[int, list[str]] | None = None) -> dict[str, Any]:
    view = sample_field_view(row)
    issue_list = (issues or {}).get(int(row.get("id") or 0), [])
    result: dict[str, Any] = {
        "id": row.get("id"),
        "status": row.get("status"),
        "pending": bool(row.get("pending")),
        "abnormal": bool(row.get("abnormal")),
        "配置编号": row.get("配置编号"),
        "数据源名称": row.get("数据源名称"),
        "采样算法": row.get("采样算法"),
        "样本比例": row.get("样本比例"),
        "随机种子": row.get("随机种子"),
        "分层字段": row.get("分层字段"),
        "输出表名": row.get("输出表名"),
        "配置状态": row.get("status"),
        # 引用数据集的当前视图（已下线时字段为空）。
        "数据集编号": view["数据集编号"],
        "引用状态": view["引用状态"],
        "字段可用": view["字段可用"],
        "数据等级": view["数据等级"],
        "数据等级名称": view["数据等级名称"],
        "更新频率": view["更新频率"],
        "可用字段": view["字段清单"],
        "输出表核对": "通过" if not issue_list else "不通过",
        "核对提示": issue_list,
    }
    # 英文别名，供外部系统按通用字段名读取。
    result["dataset_code"] = view["数据集编号"]
    result["dataset_visible"] = view["字段可用"]
    result["available_fields"] = view["字段清单"]
    result["data_level"] = view["数据等级"]
    return result


class SampleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        data_source: str | None = None,
        algorithm: str | None = None,
        reference_status: str | None = None,
        level: str | None = None,
        level_min: str | None = None,
        data_level: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("配置编号", ""))
                or keyword in str(row.get("数据源名称", ""))
                or keyword in str(row.get("采样算法", ""))
            ]
        if data_source:
            rows = [row for row in rows if data_source in str(row.get("数据源名称", ""))]
        if algorithm:
            rows = [row for row in rows if algorithm in str(row.get("采样算法", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]

        issues = output_table_issues()
        decorated = [decorate_sample_row(row, issues) for row in rows]
        if reference_status:
            decorated = [row for row in decorated if row["引用状态"] == reference_status]
        # 数据等级范围：支持 data_level / level 精确，level_min 为下限。
        wanted = level or data_level
        if wanted:
            decorated = [
                row for row in decorated
                if row["字段可用"] and str(row["数据等级"] or "").upper() == str(wanted).upper()
            ]
        if level_min:
            floor = level_rank(str(level_min).upper())
            decorated = [
                row for row in decorated
                if row["字段可用"] and level_rank(str(row["数据等级"] or "")) >= floor
            ]

        total = len(decorated)
        start = max(page - 1, 0) * size
        return decorated[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        return decorate_sample_row(row, output_table_issues())

    def get_entry_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("配置编号") or "") == code:
                return decorate_sample_row(row, output_table_issues())
        return None

    def references(self) -> list[dict[str, Any]]:
        """样本输出表引用核对清单：目录侧刷新后可直接对照字段与等级。"""
        issues = output_table_issues()
        return [decorate_sample_row(row, issues) for row in store.rows(MODULE)]

    def reference_check(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"采样配置 {entry_id} 不存在或已归档"
        view = sample_field_view(row)
        issues = output_table_issues().get(entry_id, [])
        return {
            "配置编号": row.get("配置编号"),
            "数据集编号": view["数据集编号"],
            "引用状态": view["引用状态"],
            "字段可用": view["字段可用"],
            "数据等级": view["数据等级"],
            "更新频率": view["更新频率"],
            "可用字段": view["字段清单"],
            "分层字段": row.get("分层字段"),
            "分层字段可用": (
                str(row.get("分层字段") or "").strip() in view["字段清单"]
                if str(row.get("分层字段") or "").strip()
                else True
            ),
            "输出表名": row.get("输出表名"),
            "输出表核对": "通过" if not issues else "不通过",
            "核对提示": issues,
        }, ""

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        code = reference_code(values)
        if code and resolve_catalog(code) is None:
            return None, [f"数据集编号 {code} 在数据目录中不存在，不能建立采样引用"]
        catalog_row = resolve_catalog(code) if code else None
        if catalog_row is not None and not is_viewable(catalog_row):
            return None, [f"数据集 {code} 当前为「{catalog_row.get('status')}」，不能引用已下线数据集的字段"]

        table = str(values.get("输出表名") or "").strip()
        if table and any(str(row.get("输出表名") or "").strip() == table for row in store.rows(MODULE)):
            return None, [f"输出表名 {table} 已被其他采样配置占用"]

        stratified = normalize_fields(values.get("分层字段"))
        if stratified and catalog_row is not None:
            unavailable = [field for field in stratified if field not in (catalog_row.get("字段列表") or [])]
            if unavailable:
                return None, [f"分层字段不在数据集 {code} 的字段清单里：{'、'.join(unavailable)}"]

        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            if code:
                entry["数据集编号"] = code
            for field in ("样本比例", "随机种子", "分层字段", "输出表名"):
                if field in values:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return decorate_sample_row(entry, output_table_issues()), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采样配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据采样管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 启用采样前先核对引用：已下线数据集不允许把采样配置跑起来。
        if action == "启用采样":
            view = sample_field_view(entry)
            if not view["字段可用"]:
                return None, (
                    f"配置引用的数据集 {view['数据集编号'] or '—'} {view['引用状态']}，"
                    "字段不可用，不能启用采样"
                )
        with store.lock:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return decorate_sample_row(entry, output_table_issues()), f"采样配置已{action}"


def sample_columns() -> list[str]:
    return [
        "配置编号", "数据源名称", "采样算法", "样本比例", "随机种子",
        "分层字段", "输出表名", "数据集编号", "数据等级", "引用状态", "配置状态",
    ]

"""目录字段清单的纯规则：字段规范化、数据等级重算与字段清单快照。

这一层不依赖 store / FastAPI，方便目录检索和数据采样两个服务复用同一套口径：
- 字段清单统一成去空、去重、保序的列表；
- 数据等级由字段敏感度和更新频率共同决定；
- 快照是不可变的历史，后续等级变化不会回写旧快照。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

# 数据等级从低到高。界面和后端都只认 L1~L4，中文名称只用于展示。
LEVELS = ("L1", "L2", "L3", "L4")
LEVEL_NAMES = {"L1": "公开", "L2": "内部", "L3": "敏感", "L4": "高敏感"}
LEVEL_RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
LEVEL_ALIASES = {
    "1": "L1", "2": "L2", "3": "L3", "4": "L4",
    "一级": "L1", "二级": "L2", "三级": "L3", "四级": "L4",
    "公开": "L1", "内部": "L2", "敏感": "L3", "高敏感": "L4",
}

# 只有这两个状态算“可查看”。已下线、已归档立即从可查看清单移出。
VIEWABLE_STATUSES = ("草稿", "已发布")
HIDDEN_STATUSES = ("已下线", "已归档")
ALL_STATUSES = ("草稿", "已发布", "已下线", "已归档")

ALLOWED_FREQUENCIES = ("实时", "每小时", "每日", "每周", "每月", "每季度", "每年", "未知")
# 高刷新频率会把等级抬高一级，避免“每日刷新”被过度抬级。
FREQUENCY_BUMP = {"实时": 1, "每小时": 1}

# 命中即高敏感（L4）。先判高敏感，再判普通敏感。
HIGH_SENSITIVE_HINTS = (
    "身份证", "银行卡", "银行账号", "信用卡", "密码", "薪酬", "工资",
    "病历", "健康", "生物特征", "护照", "家庭住址", "手机号", "手机号码",
    "社保", "税号", "精确位置",
)
SENSITIVE_HINTS = (
    "金额", "收入", "余额", "客户", "订单", "交易", "地址", "电话", "手机",
    "邮箱", "姓名", "支付", "账号", "账户", "证件", "合同", "优惠",
    "营业额", "客流量", "销售额", "成本",
)

FIELD_SPLIT_RE = re.compile(r"[,，、;；|\n\r\t]+")
MAX_FIELDS = 200
MAX_FIELD_NAME_LENGTH = 64
INITIAL_SNAPSHOT_AT = "2026-11-01T00:00:00+00:00"


def now_iso() -> str:
    """统一用带时区的 ISO 时间，方便快照排序和接口回显。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_fields(value: Any) -> list[str]:
    """把字符串、列表或混合输入整理成去空、去重、保序的字段列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts: Iterable[Any] = FIELD_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple, set)):
        expanded: list[Any] = []
        for item in value:
            if isinstance(item, str):
                expanded.extend(FIELD_SPLIT_RE.split(item))
            elif item is not None:
                expanded.append(item)
        parts = expanded
    else:
        parts = [value]

    fields: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = str(part).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        fields.append(name)
    return fields


def validate_fields(value: Any) -> tuple[list[str], list[str]]:
    """字段校验：空清单、超长、控制字符和数量上限都直接拒绝写入。"""
    fields = normalize_fields(value)
    errors: list[str] = []
    if not fields:
        errors.append("字段清单不能为空")
    if len(fields) > MAX_FIELDS:
        errors.append(f"字段清单最多 {MAX_FIELDS} 个字段")
    for field in fields:
        if len(field) > MAX_FIELD_NAME_LENGTH:
            errors.append(f"字段「{field[:16]}…」超过 {MAX_FIELD_NAME_LENGTH} 个字符")
        if any(ord(char) < 32 for char in field):
            errors.append(f"字段「{field[:16]}…」包含不可见控制字符")
    return fields, errors


def normalize_frequency(value: Any, default: str = "每日") -> str:
    text = str(value or "").strip()
    return text if text in ALLOWED_FREQUENCIES else default


def validate_frequency(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text not in ALLOWED_FREQUENCIES:
        return f"更新频率「{text}」不在允许范围内：{'、'.join(ALLOWED_FREQUENCIES)}"
    return None


def compute_level(fields: Iterable[str], update_frequency: str = "每日") -> str:
    """按字段敏感度定基础等级，再按更新频率抬一级（最高 L4）。"""
    field_list = list(fields)
    if not field_list:
        return "L1"
    text = " ".join(field_list)
    if any(hint in text for hint in HIGH_SENSITIVE_HINTS):
        level = "L4"
    elif any(hint in text for hint in SENSITIVE_HINTS):
        level = "L3"
    else:
        # 目录里的数据集默认按内部数据管理，不因为字段“看起来普通”就降到公开。
        level = "L2"
    rank = min(LEVEL_RANK[level] + FREQUENCY_BUMP.get(update_frequency, 0), len(LEVELS))
    return LEVELS[rank - 1]


def normalize_level(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    upper = text.upper()
    if upper in LEVEL_RANK:
        return upper
    return LEVEL_ALIASES.get(text)


def level_name(level: str | None) -> str:
    return LEVEL_NAMES.get(level or "", "")


def level_rank(level: str | None) -> int:
    return LEVEL_RANK.get(level or "", 0)


def format_fields(fields: Iterable[str]) -> str:
    return "、".join(fields)


def fields_signature(fields: Iterable[str]) -> str:
    payload = "\x1f".join(fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_snapshot(
    *,
    version: int,
    fields: Iterable[str],
    level: str,
    update_frequency: str,
    batch_id: str,
    reason: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    field_list = list(fields)
    return {
        "字段版本": version,
        "version": version,
        "字段清单": format_fields(field_list),
        "字段列表": field_list,
        "字段签名": fields_signature(field_list),
        "数据等级": level,
        "数据等级名称": level_name(level),
        "等级数值": level_rank(level),
        "更新频率": update_frequency,
        "批次号": batch_id,
        "变更原因": reason,
        "创建时间": created_at or now_iso(),
    }


def _dedupe_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一版本只留一份；连续重复的同一状态只保留最近一次。

    这样“再次更新同一字段清单”不会堆出重复版本；字段或等级变化过的历史快照
    仍然按写入当时的等级保留（字段改走后又改回来是有意义的变更历史）。
    """
    by_version: dict[int, dict[str, Any]] = {}
    for snapshot in snapshots:
        try:
            version = int(snapshot.get("字段版本", snapshot.get("version", 1)))
        except (TypeError, ValueError):
            version = 1
        snapshot["字段版本"] = version
        snapshot["version"] = version
        snapshot.setdefault("字段列表", normalize_fields(snapshot.get("字段清单")))
        snapshot.setdefault("字段清单", format_fields(snapshot["字段列表"]))
        snapshot.setdefault("字段签名", fields_signature(snapshot["字段列表"]))
        snapshot.setdefault("数据等级名称", level_name(snapshot.get("数据等级")))
        snapshot.setdefault("等级数值", level_rank(snapshot.get("数据等级")))
        snapshot.setdefault("创建时间", INITIAL_SNAPSHOT_AT)
        # 同一版本出现多份时只认最早写入的那一份。
        by_version.setdefault(version, snapshot)

    ordered = [by_version[version] for version in sorted(by_version)]
    unique: list[dict[str, Any]] = []
    previous_state: tuple[tuple[str, ...], str, str] | None = None
    for snapshot in ordered:
        state = (
            tuple(snapshot.get("字段列表") or []),
            str(snapshot.get("数据等级") or ""),
            str(snapshot.get("更新频率") or ""),
        )
        if state == previous_state:
            # 连续重复：用最近一次写入的快照覆盖前一条，版本号仍然唯一。
            unique[-1] = snapshot
            continue
        previous_state = state
        unique.append(snapshot)
    return unique


def level_basis(fields: Iterable[str], update_frequency: str) -> str:
    """等级重算的判据：字段签名 + 更新频率。判据没变就不重算。"""
    return f"{fields_signature(fields)}|{update_frequency}"


def ensure_row_state(row: dict[str, Any]) -> dict[str, Any]:
    """把一行目录数据补齐成增量更新需要的样子（幂等，且不重复重算等级）。

    只有字段清单或更新频率与上次重算时的判据不一致时才重算数据等级，
    读数路径因此不会把没受影响的条目一起重算。
    """
    raw_fields = row.get("字段列表")
    if raw_fields is None:
        raw_fields = row.get("字段清单")
    fields = normalize_fields(raw_fields)

    row["字段列表"] = fields
    row["字段清单"] = format_fields(fields)

    try:
        version = int(row.get("字段版本", row.get("field_version", 1)) or 1)
    except (TypeError, ValueError):
        version = 1
    version = max(version, 1)
    row["字段版本"] = version
    row["field_version"] = version

    frequency = normalize_frequency(row.get("更新频率"))
    row["更新频率"] = frequency
    row["update_frequency"] = frequency

    basis = level_basis(fields, frequency)
    if not row.get("数据等级") or row.get("等级基准签名") != basis:
        level = compute_level(fields, frequency)
        row["数据等级"] = level
        row["数据等级名称"] = level_name(level)
        row["等级数值"] = level_rank(level)
        row["data_level"] = level
        row["等级基准签名"] = basis
        row.setdefault("等级重算批次", "初始版本")
    else:
        # 保持已有标记，不覆盖“哪个批次重算过”的审计信息。
        row["数据等级名称"] = level_name(row["数据等级"])
        row["等级数值"] = level_rank(row["数据等级"])
        row["data_level"] = row["数据等级"]

    row["field_list"] = fields
    row["可见"] = row.get("status") not in HIDDEN_STATUSES
    row["条目状态"] = row.get("status")
    row.setdefault("更新时间", INITIAL_SNAPSHOT_AT)
    row.setdefault("最近批次号", "初始版本")

    snapshots = row.setdefault("字段清单快照", [])
    snapshots[:] = _dedupe_snapshots(snapshots)
    if not snapshots:
        snapshots.append(
            make_snapshot(
                version=version,
                fields=fields,
                level=row["数据等级"],
                update_frequency=frequency,
                batch_id=str(row.get("最近批次号") or "初始版本"),
                reason="初始化快照",
                created_at=str(row.get("更新时间") or INITIAL_SNAPSHOT_AT),
            )
        )
    return row


def ensure_catalog_state() -> list[dict[str, Any]]:
    """延迟导入 store，避免 catalog_fields 和 store 形成循环依赖。"""
    from app.store import store

    with store.lock:
        rows = store.rows("catalog")
        for row in rows:
            ensure_row_state(row)
    return rows


def decorate_catalog_row(row: dict[str, Any], *, include_snapshots: bool = False) -> dict[str, Any]:
    """输出给接口的目录条目：保留中文业务字段，同时给出英文别名方便对接。"""
    ensure_row_state(row)
    fields = list(row["字段列表"])
    result: dict[str, Any] = {
        "id": row.get("id"),
        "status": row.get("status"),
        "pending": bool(row.get("pending")),
        "abnormal": bool(row.get("abnormal")),
        "条目编号": row.get("条目编号"),
        "数据集名称": row.get("数据集名称"),
        "业务域": row.get("业务域"),
        "字段清单": format_fields(fields),
        "字段列表": fields,
        "字段版本": int(row["字段版本"]),
        "字段签名": fields_signature(fields),
        "更新频率": row.get("更新频率"),
        "数据等级": row.get("数据等级"),
        "数据等级名称": row.get("数据等级名称"),
        "等级数值": row.get("等级数值"),
        "负责人": row.get("负责人"),
        "条目状态": row.get("条目状态") or row.get("status"),
        "可见": bool(row.get("可见")),
        "更新时间": row.get("更新时间"),
        "下线时间": row.get("下线时间"),
        "最近批次号": row.get("最近批次号"),
        # 英文别名：外部系统/测试用字段名读写时不至于对不上。
        "field_list": fields,
        "field_version": int(row["字段版本"]),
        "data_level": row.get("数据等级"),
        "update_frequency": row.get("更新频率"),
    }
    if include_snapshots:
        result["字段清单快照"] = snapshot_view(row)
    return result


def snapshot_view(row: dict[str, Any], *, include_current: bool = True) -> list[dict[str, Any]]:
    """快照按版本升序返回，当前版本额外标记；旧快照的等级保持当时的值。"""
    ensure_row_state(row)
    current_version = int(row["字段版本"])
    snapshots: list[dict[str, Any]] = []
    for snapshot in row.get("字段清单快照", []):
        item = dict(snapshot)
        item["字段列表"] = list(item.get("字段列表") or [])
        item["字段清单"] = format_fields(item["字段列表"])
        item["当前"] = int(item.get("字段版本", 1)) == current_version
        if item["当前"] or include_current:
            snapshots.append(item)
    # 理论上 ensure 已去重；这里再做一次版本去重，保证“同一字段清单只留一个版本”。
    deduped: dict[int, dict[str, Any]] = {}
    for item in snapshots:
        deduped.setdefault(int(item.get("字段版本", 1)), item)
    return [deduped[version] for version in sorted(deduped)]


def current_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snapshots = snapshot_view(row)
    if not snapshots:
        return {}
    return snapshots[-1]


def settled_marker(row: dict[str, Any]) -> dict[str, Any]:
    """并发冲突时返回给落后批次的“已落定标记”。"""
    ensure_row_state(row)
    snapshot = current_snapshot(row)
    return {
        "条目编号": row.get("条目编号"),
        "字段版本": int(row["字段版本"]),
        "字段清单": row.get("字段清单"),
        "字段列表": list(row.get("字段列表") or []),
        "字段签名": fields_signature(row.get("字段列表") or []),
        "数据等级": row.get("数据等级"),
        "数据等级名称": row.get("数据等级名称"),
        "更新频率": row.get("更新频率"),
        "批次号": row.get("最近批次号"),
        "落定时间": row.get("更新时间"),
        "快照": snapshot,
    }

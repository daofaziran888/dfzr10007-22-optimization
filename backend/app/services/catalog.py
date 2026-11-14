"""数据目录检索业务规则：状态流转、字段校验与筛选口径都收在这里。

增量更新的几条硬规则：
- 字段清单变化后按业务域、条目编号重新排序并定位受影响记录，其余条目不动；
- 数据等级随字段清单与更新频率重算，历史快照保留当时的等级；
- 已下线/已归档立即从可查看清单移出，单条明细仍可追溯；
- 同一份字段清单重复提交只保留一个版本（按字段签名去重）；
- 并发批次以字段校验和锁内版本号为准，先通过校验的一份写入，后来者拿到已落定标记。
"""
from __future__ import annotations

import copy
from typing import Any, Iterable

from app.store import store
from app.services.catalog_fields import (
    HIDDEN_STATUSES,
    VIEWABLE_STATUSES,
    compute_level,
    decorate_catalog_row,
    ensure_catalog_state,
    ensure_row_state,
    fields_signature,
    format_fields,
    level_name,
    level_rank,
    make_snapshot,
    normalize_frequency,
    normalize_fields,
    normalize_level,
    now_iso,
    settled_marker,
    snapshot_view,
    validate_fields,
    validate_frequency,
)

MODULE = "catalog"
REQUIRED_FIELDS = ["条目编号", "数据集名称", "业务域", "字段清单", "更新频率"]
STATUS_ORDER = ["草稿", "已发布", "已下线", "已归档"]
# 动作 → 目标状态；只有前置状态匹配时才允许流转。
ACTION_RULES = {"发布条目": "已发布", "下线条目": "已下线", "归档条目": "已归档"}
ACTION_FROM = {"发布条目": "草稿", "下线条目": "已发布", "归档条目": "已下线"}
NEGATIVE_ACTIONS = ["下线条目"]

# 正在写入字段清单的条目：同一数据集同一时刻只允许一个批次落库。
INFLIGHT: set[int] = set()


def sort_catalog_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按业务域、条目编号、主键排序，保证同一份数据每次呈现顺序稳定。"""
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("业务域") or ""),
            str(row.get("条目编号") or ""),
            int(row.get("id") or 0),
        ),
    )


def is_viewable(row: dict[str, Any]) -> bool:
    """已下线、已归档的数据集立即视为不可查看。"""
    return str(row.get("status") or "") in VIEWABLE_STATUSES


class CatalogService:
    def __init__(self) -> None:
        ensure_catalog_state()

    # ------------------------------------------------------------------ 列表
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        business_domain: str | None = None,
        keyword_field: str | None = None,
        level: str | None = None,
        level_min: str | None = None,
        include_hidden: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [copy.deepcopy(row) for row in ensure_catalog_state()]

        # 默认只给可查看清单；已下线条目要显式 include_hidden 才能看见。
        if status:
            rows = [row for row in rows if row.get("status") == status]
        elif not include_hidden:
            rows = [row for row in rows if is_viewable(row)]

        if business_domain:
            rows = [row for row in rows if business_domain in str(row.get("业务域") or "")]
        if keyword:
            if keyword_field in ("条目编号", "数据集名称", "业务域"):
                # 指定字段检索时只在该字段里匹配，和界面的筛选框一一对应。
                rows = [
                    row for row in rows
                    if keyword in str(row.get(keyword_field) or "")
                ]
            else:
                rows = [
                    row for row in rows
                    if keyword in str(row.get("条目编号") or "")
                    or keyword in str(row.get("数据集名称") or "")
                    or keyword in str(row.get("业务域") or "")
                ]
        if level:
            target = normalize_level(level)
            rows = [row for row in rows if target is None or row.get("数据等级") == target]
        if level_min:
            floor = normalize_level(level_min)
            if floor is not None:
                rows = [
                    row for row in rows
                    if level_rank(str(row.get("数据等级") or "")) >= level_rank(floor)
                ]

        rows = sort_catalog_rows(rows)
        total = len(rows)
        start = max(page - 1, 0) * size
        items = [
            decorate_catalog_row(row)
            for row in rows[start:start + size]
        ]
        return items, total

    def visible_entries(self) -> list[dict[str, Any]]:
        """可查看清单（不含分页），供导出与采样侧引用校验。"""
        rows = [row for row in ensure_catalog_state() if is_viewable(row)]
        return [decorate_catalog_row(row, include_snapshots=True) for row in sort_catalog_rows(rows)]

    def hidden_entries(self) -> list[dict[str, Any]]:
        rows = [row for row in ensure_catalog_state() if not is_viewable(row)]
        return [decorate_catalog_row(row) for row in sort_catalog_rows(rows)]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        ensure_row_state(row)
        return decorate_catalog_row(row, include_snapshots=True)

    def get_entry_by_code(self, code: str) -> dict[str, Any] | None:
        for row in ensure_catalog_state():
            if str(row.get("条目编号") or "") == code:
                return decorate_catalog_row(row, include_snapshots=True)
        return None

    def snapshots(self, entry_id: int) -> tuple[list[dict[str, Any]], str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return [], f"目录条目 {entry_id} 不存在或已归档"
        return snapshot_view(row), None

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        frequency_error = validate_frequency(values.get("更新频率"))
        if frequency_error:
            return None, [frequency_error]
        fields, field_errors = validate_fields(values.get("字段清单"))
        if field_errors:
            return None, field_errors

        code = str(values.get("条目编号") or "").strip()
        with store.lock:
            rows = store.rows(MODULE)
            if any(str(row.get("条目编号") or "") == code for row in rows):
                return None, [f"条目编号 {code} 已存在，请换一个编号"]

            frequency = normalize_frequency(values.get("更新频率"))
            level = compute_level(fields, frequency)
            entry = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "条目编号": code,
                "数据集名称": str(values.get("数据集名称") or "").strip(),
                "业务域": str(values.get("业务域") or "").strip(),
                "字段清单": format_fields(fields),
                "字段列表": fields,
                "字段版本": 1,
                "field_version": 1,
                "更新频率": frequency,
                "update_frequency": frequency,
                "数据等级": level,
                "数据等级名称": level_name(level),
                "等级数值": level_rank(level),
                "data_level": level,
                "等级基准签名": "",
                "负责人": str(values.get("负责人") or "").strip(),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "可见": True,
                "最近批次号": str(values.get("批次号") or "登记"),
                "更新时间": now_iso(),
                "字段清单快照": [],
            }
            ensure_row_state(entry)
            entry["等级重算批次"] = entry["最近批次号"]
            entry["字段清单快照"][0]["创建时间"] = entry["更新时间"]
            rows.append(entry)
        return decorate_catalog_row(entry, include_snapshots=True), []

    # --------------------------------------------------- 字段清单增量更新
    def update_fields(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[str], dict[str, Any] | None]:
        """重算单条目录条目的字段清单、数据等级与快照。

        返回 (条目, 错误列表, 冲突信息)。先通过字段校验且版本仍然一致的批次写入，
        版本已变的批次不落库，只返回当前已落定标记。
        """
        return self._apply_field_change(entry_id, values, require_change=True)

    def preview_fields(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """只算不写：给前端展示重算后的等级，避免“试算”也产生版本。"""
        entry, errors, _ = self._apply_field_change(entry_id, values, require_change=False, dry_run=True)
        return entry, errors

    def _apply_field_change(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        require_change: bool,
        dry_run: bool = False,
    ) -> tuple[dict[str, Any] | None, list[str], dict[str, Any] | None]:
        # 1) 先做字段校验：校验不过的批次连写入资格都拿不到。
        errors: list[str] = []
        frequency_error = validate_frequency(values.get("更新频率"))
        if frequency_error:
            errors.append(frequency_error)
        fields, field_errors = validate_fields(values.get("字段清单"))
        errors.extend(field_errors)

        batch_id = str(values.get("批次号") or values.get("batch_id") or "").strip()
        if not batch_id:
            errors.append("缺少批次号，无法定位并发更新的先后顺序")
        raw_version = values.get("字段版本", values.get("field_version", values.get("expected_version")))
        expected_version: int | None = None
        if raw_version is not None:
            try:
                expected_version = int(raw_version)
            except (TypeError, ValueError):
                errors.append(f"字段版本「{raw_version}」不是整数")
        if errors:
            return None, errors, None

        with store.lock:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, [f"目录条目 {entry_id} 不存在或已归档"], None
            ensure_row_state(row)
            # 进入更新即登记为在途：同类批次在这段窗口内到达时只能让位。
            if entry_id in INFLIGHT:
                return None, [], {
                    "原因": "并发更新冲突：同一数据集已有批次正在写入",
                    "期望版本": expected_version,
                    "已落定": settled_marker(row),
                }
            observed_version = int(row["字段版本"])
            INFLIGHT.add(entry_id)

        try:
            if expected_version is None:
                # 没显式带版本：以本批次开始更新时读到的版本为基准。
                # 两个并发批次通常读到同一个版本，只有一个能继续写入。
                expected_version = observed_version

            with store.lock:
                row = store.find(MODULE, entry_id)
                if row is None:
                    return None, [f"目录条目 {entry_id} 不存在或已归档"], None
                ensure_row_state(row)
                current_version = int(row["字段版本"])

                # 2) 版本已推进说明另一批次先落定：本次不写，只读已落定标记。
                if expected_version != current_version:
                    return None, [], {
                        "原因": "并发更新冲突：该条目已被先通过字段校验的批次写入",
                        "期望版本": expected_version,
                        "已落定": settled_marker(row),
                    }

            current_fields = list(row.get("字段列表") or [])
            signature = fields_signature(fields)
            frequency = normalize_frequency(
                values.get("更新频率"), default=normalize_frequency(row.get("更新频率"))
            )

            unchanged = fields == current_fields and frequency == row.get("更新频率")
            changed = fields != current_fields
            frequency_changed = frequency != row.get("更新频率")

            if require_change and unchanged and not values.get("强制更新"):
                # 同一份字段清单重复提交：不再新开版本，沿用最近一个快照。
                return decorate_catalog_row(row, include_snapshots=True), [], None

            target_version = current_version if unchanged else current_version + 1
            level = compute_level(fields, frequency)

            if dry_run:
                preview = copy.deepcopy(row)
                preview.update({
                    "字段列表": fields,
                    "字段清单": format_fields(fields),
                    "字段版本": target_version,
                    "更新频率": frequency,
                    "数据等级": level,
                    "数据等级名称": level_name(level),
                    "等级数值": level_rank(level),
                })
                preview["重算"] = changed or frequency_changed
                return decorate_catalog_row(preview, include_snapshots=True), [], None

            # 3) 写入：版本、等级、快照一起更新。
            row["字段列表"] = fields
            row["字段清单"] = format_fields(fields)
            row["更新频率"] = frequency
            row["update_frequency"] = frequency
            row["字段版本"] = target_version
            row["field_version"] = target_version
            row["数据等级"] = level
            row["数据等级名称"] = level_name(level)
            row["等级数值"] = level_rank(level)
            row["data_level"] = level
            row["等级基准签名"] = f"{signature}|{frequency}"
            row["等级重算批次"] = batch_id
            row["最近批次号"] = batch_id
            row["更新时间"] = now_iso()
            if "负责人" in values:
                row["负责人"] = str(values.get("负责人") or "").strip()

            if changed or frequency_changed:
                reason = "字段清单变更" if changed else "更新频率变更"
                snapshots = row.setdefault("字段清单快照", [])
                # 历史快照不删除：字段回退时按新版本记录，旧版本的当时等级仍然可追溯。
                snapshots.append(
                    make_snapshot(
                        version=target_version,
                        fields=fields,
                        level=level,
                        update_frequency=frequency,
                        batch_id=batch_id,
                        reason=reason,
                    )
                )
            else:
                # 强制重算同一份清单：只刷新当前快照的等级，不新增版本。
                snapshots = row.get("字段清单快照", [])
                for snapshot in snapshots:
                    if int(snapshot.get("字段版本", 0)) == target_version:
                        snapshot["数据等级"] = level
                        snapshot["数据等级名称"] = level_name(level)
                        snapshot["等级数值"] = level_rank(level)
                        break

            return decorate_catalog_row(row, include_snapshots=True), [], None
        finally:
            INFLIGHT.discard(entry_id)

    def batch_update_fields(
        self,
        updates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量字段更新：逐条走单条规则，只重算字段变化影响到的记录。

        每条结果都写回 affected / unchanged / conflict / invalid，调用方不用再猜。
        """
        results: list[dict[str, Any]] = []
        for update in updates:
            raw_id = update.get("id", update.get("entry_id"))
            try:
                entry_id = int(raw_id)
            except (TypeError, ValueError):
                results.append({
                    "id": raw_id,
                    "outcome": "invalid",
                    "errors": [f"条目 id「{raw_id}」不是整数"],
                })
                continue
            current_row = store.find(MODULE, entry_id)
            before_version = (
                int(current_row.get("字段版本") or 0) if current_row is not None else None
            )
            entry, errors, conflict = self._apply_field_change(
                entry_id, update, require_change=True
            )
            after_version = None if entry is None else int(entry.get("字段版本") or 0)
            if conflict is not None:
                results.append({
                    "id": entry_id,
                    "outcome": "conflict",
                    "errors": [],
                    "conflict": conflict,
                })
            elif errors:
                results.append({"id": entry_id, "outcome": "invalid", "errors": errors})
            else:
                changed = (
                    before_version is not None
                    and after_version is not None
                    and after_version != before_version
                )
                results.append({
                    "id": entry_id,
                    "outcome": "affected" if changed else "unchanged",
                    "entry": entry,
                })
        return results

    # ---------------------------------------------------------------- 刷新
    def refresh(self, values: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """按增量更新重算并重新定位：只处理命中的条目，返回受影响清单。

        values 支持：
        - entry_id / entry_code / 业务域 / 字段变更列表
        - recalculate_all=true 才会对全部可见条目做一次巡检式重算
        """
        targets: list[dict[str, Any]] = []
        if "entry_id" in values:
            try:
                entry_id = int(values["entry_id"])
            except (TypeError, ValueError):
                return [], [f"条目 id「{values['entry_id']}」不是整数"]
            row = store.find(MODULE, entry_id)
            if row is None:
                return [], [f"目录条目 {entry_id} 不存在或已归档"]
            targets = [row]
        elif values.get("entry_code"):
            code = str(values["entry_code"]).strip()
            targets = [row for row in ensure_catalog_state() if str(row.get("条目编号") or "") == code]
            if not targets:
                return [], [f"条目编号 {code} 不存在"]
        elif values.get("business_domain"):
            domain = str(values["business_domain"]).strip()
            targets = [row for row in ensure_catalog_state() if str(row.get("业务域") or "") == domain]
            if not targets:
                return [], [f"业务域「{domain}」下没有目录条目"]
        elif values.get("recalculate_all"):
            targets = [row for row in ensure_catalog_state() if is_viewable(row)]
        else:
            return [], ["请提供 entry_id、条目编号、业务域，或 recalculate_all=true"]

        updates = values.get("字段变更") or []
        if not isinstance(updates, list):
            return [], ["字段变更必须是列表，每项包含 id 与字段清单"]

        # 指定了 entry_id / 条目编号 / 业务域时，字段变更只作用于命中的条目，
        # 避免“刷新 CATA-0002”的请求顺带改到别的数据集。
        target_ids = {int(row.get("id") or 0) for row in targets}
        scoped_updates: list[dict[str, Any]] = []
        skipped: list[int] = []
        for update in updates:
            if not isinstance(update, dict):
                return [], ["字段变更必须是对象列表，每项包含 id 与字段清单"]
            raw_id = update.get("id", update.get("entry_id"))
            try:
                update_id = int(raw_id)
            except (TypeError, ValueError):
                return [], [f"字段变更里的条目 id「{raw_id}」不是整数"]
            if target_ids and update_id not in target_ids:
                skipped.append(update_id)
                continue
            scoped_updates.append(update)

        results = self.batch_update_fields(scoped_updates) if scoped_updates else []
        changed_ids = {
            int(item["id"]) for item in results
            if item.get("outcome") == "affected" and "entry" in item
        }
        if not updates:
            # 没有字段变更时只做等级巡检：只有等级基准与实际字段/频率不一致的条目
            # 才会重算，其余条目连版本和快照都不动。
            stale_ids: set[int] = set()
            for row in targets:
                fields = normalize_fields(row.get("字段列表", row.get("字段清单")))
                frequency = normalize_frequency(row.get("更新频率"))
                basis = f"{fields_signature(fields)}|{frequency}"
                if not row.get("数据等级") or row.get("等级基准签名") != basis:
                    stale_ids.add(int(row.get("id") or 0))
                ensure_row_state(row)
            current = sort_catalog_rows(
                row for row in ensure_catalog_state()
                if int(row.get("id") or 0) in stale_ids
            )
            return [
                {**decorate_catalog_row(row), "定位序号": index + 1, "重算": True}
                for index, row in enumerate(current)
            ], []

        # 4) 字段变化后按业务域排序并定位受影响的目录记录。
        affected_rows = [row for row in ensure_catalog_state() if int(row.get("id") or 0) in changed_ids]
        reordered = sort_catalog_rows(affected_rows)
        affected = [
            {
                **decorate_catalog_row(row, include_snapshots=True),
                "定位序号": index + 1,
            }
            for index, row in enumerate(reordered)
        ]
        if skipped:
            affected.append({"未处理": skipped, "原因": "字段变更的条目不在本次刷新范围内"})
        return affected, []

    # ---------------------------------------------------------------- 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"目录条目 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据目录检索可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.lock:
            current = str(entry.get("status") or "")
            required = ACTION_FROM[action]
            if current != required:
                return None, f"动作「{action}」要求条目当前为「{required}」，当前是「{current}」"
            entry["status"] = target
            entry["条目状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            # 下线即刷新可见性：不依赖下一次列表读取再兜底。
            entry["可见"] = target not in HIDDEN_STATUSES
            if target in HIDDEN_STATUSES:
                entry["下线时间"] = now_iso()
            else:
                entry["下线时间"] = None
        message = f"目录条目已{action}"
        if target in HIDDEN_STATUSES:
            message += "，已移出可查看的数据目录清单"
        return decorate_catalog_row(entry, include_snapshots=True), message


def catalog_list_fields() -> list[str]:
    """列表字段口径：线上清单只暴露可查看条目的字段。"""
    return ["条目编号", "数据集名称", "业务域", "字段清单", "更新频率", "数据等级", "负责人", "条目状态"]

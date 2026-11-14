"""目录增量更新链路的回归用例。

用标准库 unittest 写，不引入额外依赖：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.catalog import CatalogService  # noqa: E402
from app.services.catalog_fields import (  # noqa: E402
    compute_level,
    normalize_fields,
    validate_fields,
)
from app.services.sample import SampleService  # noqa: E402
from app.store import store  # noqa: E402


class CatalogIncrementalTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.catalog = CatalogService()
        self.sample = SampleService()

    def visible_codes(self) -> list[str]:
        items, _ = self.catalog.list_entries(page=1, size=100)
        return [item["条目编号"] for item in items]

    def test_list_sorted_by_business_domain_and_hides_offline(self) -> None:
        items, total = self.catalog.list_entries(page=1, size=100)
        domains = [item["业务域"] for item in items]
        self.assertEqual(domains, sorted(domains))
        self.assertEqual(total, 4)
        self.assertNotIn("CATA-0003", [item["条目编号"] for item in items])
        self.assertEqual([item["条目编号"] for item in self.catalog.hidden_entries()], ["CATA-0003"])

    def test_offline_action_moves_entry_out_of_view_immediately(self) -> None:
        entry, message = self.catalog.run_action(4, "下线条目")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["可见"])
        self.assertIn("移出可查看", message)
        self.assertNotIn("CATA-0004", self.visible_codes())
        # 仍可按条目追溯，但不能从可查看清单/导出里看到。
        self.assertIsNotNone(self.catalog.get_entry(4))
        exported, _ = self.catalog.list_entries(page=1, size=10000)
        self.assertNotIn("CATA-0004", [item["条目编号"] for item in exported])

    def test_field_change_only_recomputes_target_entry(self) -> None:
        before = {
            item["条目编号"]: (item["字段版本"], item["数据等级"])
            for item in self.catalog.list_entries(page=1, size=100)[0]
        }
        entry, errors, conflict = self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B1"}
        )
        self.assertEqual(errors, [])
        self.assertIsNone(conflict)
        self.assertEqual(entry["条目编号"], "CATA-0002")
        self.assertEqual(entry["字段版本"], before["CATA-0002"][0] + 1)

        after = {
            item["条目编号"]: (item["字段版本"], item["数据等级"])
            for item in self.catalog.list_entries(page=1, size=100)[0]
        }
        for code, (version, level) in before.items():
            if code == "CATA-0002":
                continue
            self.assertEqual(after[code], (version, level), f"{code} 不应被重算")

    def test_level_follows_fields_and_frequency(self) -> None:
        entry, _, _ = self.catalog.update_fields(
            5, {"字段清单": "商品编号、客户姓名", "批次号": "B2", "更新频率": "每日"}
        )
        self.assertEqual(entry["数据等级"], "L3")
        entry, _, _ = self.catalog.update_fields(
            5, {"字段清单": "商品编号、客户姓名", "批次号": "B3", "更新频率": "实时"}
        )
        self.assertEqual(entry["数据等级"], "L4")
        entry, _, _ = self.catalog.update_fields(
            5, {"字段清单": "商品编号、商品名称", "批次号": "B4", "更新频率": "每月"}
        )
        self.assertEqual(entry["数据等级"], "L2")

    def test_snapshots_keep_historical_levels(self) -> None:
        self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B5"}
        )
        self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、注册时间、客户等级", "批次号": "B6"}
        )
        snapshots, error = self.catalog.snapshots(2)
        self.assertIsNone(error)
        self.assertEqual([item["字段版本"] for item in snapshots], sorted(item["字段版本"] for item in snapshots))
        # 版本 1 是新增手机号之前的历史快照，等级必须还是当时的 L3。
        self.assertEqual(snapshots[0]["数据等级"], "L3")
        self.assertEqual(snapshots[0]["字段清单"], "客户编号、客户姓名、注册时间")
        self.assertEqual(snapshots[-1]["数据等级"], self.catalog.get_entry(2)["数据等级"])
        self.assertTrue(snapshots[-1]["当前"])

    def test_duplicate_field_list_keeps_single_version(self) -> None:
        payload = {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B7"}
        entry, _, _ = self.catalog.update_fields(2, payload)
        version = entry["字段版本"]
        entry_again, errors, conflict = self.catalog.update_fields(2, payload)
        self.assertEqual(errors, [])
        self.assertIsNone(conflict)
        self.assertEqual(entry_again["字段版本"], version)
        snapshots, _ = self.catalog.snapshots(2)
        self.assertEqual(sum(1 for item in snapshots if item["字段版本"] == version), 1)
        # 换成相同字段集合的不同写法也只留一个版本。
        entry_same, _, _ = self.catalog.update_fields(
            2, {"字段清单": ["客户编号", "客户姓名", "手机号", "注册时间", "客户等级", "客户编号"], "批次号": "B8"}
        )
        self.assertEqual(entry_same["字段版本"], version)
        snapshots, _ = self.catalog.snapshots(2)
        self.assertEqual([item["字段版本"] for item in snapshots], sorted({item["字段版本"] for item in snapshots}))

    def test_conflict_only_first_valid_batch_writes(self) -> None:
        base_version = self.catalog.get_entry(2)["字段版本"]
        first, errors, conflict = self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B9", "字段版本": base_version}
        )
        self.assertEqual(errors, [])
        self.assertIsNone(conflict)
        second, errors2, conflict2 = self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名", "批次号": "B10", "字段版本": base_version}
        )
        self.assertIsNone(second)
        self.assertEqual(errors2, [])
        self.assertIsNotNone(conflict2)
        settled = conflict2["已落定"]
        self.assertEqual(settled["字段版本"], first["字段版本"])
        self.assertEqual(settled["字段清单"], first["字段清单"])
        self.assertEqual(self.catalog.get_entry(2)["字段清单"], first["字段清单"])

    def test_invalid_batch_never_writes(self) -> None:
        before = self.catalog.get_entry(2)
        entry, errors, conflict = self.catalog.update_fields(
            2, {"字段清单": "  ", "批次号": "B11"}
        )
        self.assertIsNone(entry)
        self.assertTrue(errors)
        self.assertIsNone(conflict)
        self.assertEqual(self.catalog.get_entry(2)["字段版本"], before["字段版本"])

    def test_concurrent_batches_exactly_one_wins(self) -> None:
        base_version = self.catalog.get_entry(2)["字段版本"]
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def attempt(batch: str, fields: str) -> None:
            barrier.wait()
            entry, errors, conflict = self.catalog.update_fields(
                2,
                {"字段清单": fields, "批次号": batch, "字段版本": base_version},
            )
            outcomes.append((batch, "conflict" if conflict else ("ok" if entry else "invalid")))

        threads = [
            threading.Thread(target=attempt, args=("T-1", "客户编号、客户姓名、手机号、注册时间、客户等级")),
            threading.Thread(target=attempt, args=("T-2", "客户编号、客户姓名、手机号、注册时间、客户等级")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [batch for batch, outcome in outcomes if outcome == "ok"]
        self.assertEqual(len(winners), 1, outcomes)
        final = self.catalog.get_entry(2)
        self.assertEqual(final["字段版本"], base_version + 1)
        self.assertEqual(final["最近批次号"], winners[0])

    def test_overlapping_batch_reads_settled_marker(self) -> None:
        """同一数据集已有在途批次时，后到批次不写，只读已落定标记。"""
        from app.services import catalog as catalog_module

        prior, _, _ = self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "T-FIRST"}
        )
        self.assertIsNotNone(prior)
        catalog_module.INFLIGHT.add(2)
        try:
            entry, errors, conflict = self.catalog.update_fields(
                2, {"字段清单": "客户编号、客户姓名", "批次号": "T-SECOND"}
            )
        finally:
            catalog_module.INFLIGHT.discard(2)
        self.assertIsNone(entry)
        self.assertEqual(errors, [])
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["已落定"]["字段版本"], prior["字段版本"])
        self.assertEqual(conflict["已落定"]["字段清单"], prior["字段清单"])
        self.assertEqual(self.catalog.get_entry(2)["字段清单"], prior["字段清单"])

    def test_sequential_batches_without_version_read_settled_state_each_time(self) -> None:
        """不显式带版本时按“进入更新时读到的版本”为基准，串行批次各自落定。"""
        first, errors, conflict = self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "S-1"}
        )
        self.assertEqual(errors, [])
        self.assertIsNone(conflict)
        second, errors, conflict = self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、注册时间", "批次号": "S-2"}
        )
        self.assertEqual(errors, [])
        self.assertIsNone(conflict)
        self.assertEqual(second["字段版本"], first["字段版本"] + 1)
        self.assertEqual(self.catalog.get_entry(2)["最近批次号"], "S-2")

    def test_batch_update_reports_unaffected(self) -> None:
        results = self.catalog.batch_update_fields([
            {"id": 2, "字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B12"},
            {"id": 1, "字段清单": "订单编号、交易金额、支付时间", "批次号": "B12"},
            {"id": 1, "字段清单": "", "批次号": "B12"},
        ])
        self.assertEqual([item["outcome"] for item in results], ["affected", "unchanged", "invalid"])

    def test_refresh_sorts_affected_by_business_domain(self) -> None:
        affected, errors = self.catalog.refresh({
            "recalculate_all": True,
            "字段变更": [
                {"id": 5, "字段清单": "商品编号、商品名称、客户等级", "批次号": "B13"},
                {"id": 2, "字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B13"},
            ],
        })
        self.assertEqual(errors, [])
        domains = [item["业务域"] for item in affected]
        self.assertEqual(domains, sorted(domains))
        self.assertEqual([item["定位序号"] for item in affected], [1, 2])
        self.assertEqual(
            sorted(item["条目编号"] for item in affected),
            ["CATA-0002", "CATA-0005"],
        )

    def test_field_search_is_field_scoped(self) -> None:
        items, total = self.catalog.list_entries(keyword="客户", keyword_field="数据集名称", page=1, size=100)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["条目编号"], "CATA-0002")
        # 只在业务域里匹配时，数据集名称含“客户”但业务域不含的条目不应命中。
        items, total = self.catalog.list_entries(keyword="客户", keyword_field="业务域", page=1, size=100)
        self.assertEqual([item["条目编号"] for item in items], ["CATA-0002"])

    def test_refresh_only_applies_changes_within_target_scope(self) -> None:
        affected, errors = self.catalog.refresh({
            "entry_code": "CATA-0002",
            "字段变更": [
                {"id": 2, "字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "R-1"},
                {"id": 1, "字段清单": "订单编号、交易金额", "批次号": "R-1"},
            ],
        })
        self.assertEqual(errors, [])
        self.assertEqual([item["条目编号"] for item in affected if "条目编号" in item], ["CATA-0002"])
        self.assertEqual(self.catalog.get_entry(1)["字段清单"], "订单编号、交易金额、支付时间")
        self.assertTrue(any(item.get("未处理") == [1] for item in affected))

    def test_level_filter_survives_reentry(self) -> None:
        self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "B14", "更新频率": "实时"}
        )
        items, total = self.catalog.list_entries(level_min="L4", page=1, size=100)
        self.assertGreaterEqual(total, 1)
        self.assertTrue(all(int(item["等级数值"]) >= 4 for item in items))
        again, _ = self.catalog.list_entries(level_min="L4", page=1, size=100)
        self.assertEqual([item["条目编号"] for item in again], [item["条目编号"] for item in items])


class SampleReferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.catalog = CatalogService()
        self.sample = SampleService()

    def sample_row(self, code: str) -> dict:
        items, _ = self.sample.list_entries(page=1, size=100)
        return next(item for item in items if item["配置编号"] == code)

    def test_sample_reads_current_catalog_fields(self) -> None:
        self.catalog.update_fields(
            2, {"字段清单": "客户编号、客户姓名、手机号、注册时间、客户等级", "批次号": "S1"}
        )
        row = self.sample_row("SAMP-0001")
        self.assertEqual(row["引用状态"], "引用有效")
        self.assertIn("客户等级", row["可用字段"])
        self.assertEqual(row["数据等级"], "L4")

    def test_offline_dataset_blocks_sample_fields(self) -> None:
        # CATA-0003 在示例数据里已下线。
        row = self.sample_row("SAMP-0003")
        self.assertEqual(row["引用状态"], "引用已下线")
        self.assertEqual(row["可用字段"], [])
        self.assertFalse(row["字段可用"])
        self.assertEqual(row["输出表核对"], "不通过")
        entry, message = self.sample.run_action(3, "启用采样")
        self.assertIsNone(entry)
        self.assertIn("不能启用", message)

    def test_offline_after_refresh_removes_field_access(self) -> None:
        self.assertTrue(self.sample_row("SAMP-0002")["字段可用"])
        self.catalog.run_action(1, "发布条目")
        entry, message = self.catalog.run_action(1, "下线条目")
        self.assertIsNotNone(entry)
        self.assertIn("移出可查看", message)
        row = self.sample_row("SAMP-0002")
        self.assertFalse(row["字段可用"])
        self.assertEqual(row["可用字段"], [])
        check, error = self.sample.reference_check(2)
        self.assertEqual(error, "")
        self.assertFalse(check["字段可用"])
        self.assertNotEqual(check["输出表核对"], "通过")

    def test_cannot_create_reference_to_offline_dataset(self) -> None:
        entry, errors = self.sample.create_entry({
            "配置编号": "SAMP-9001",
            "数据源名称": "CATA-0003",
            "数据集编号": "CATA-0003",
            "采样算法": "随机抽样",
            "输出表名": "tmp_sample_9001",
        })
        self.assertIsNone(entry)
        self.assertTrue(errors)

    def test_cannot_create_duplicate_output_table(self) -> None:
        entry, errors = self.sample.create_entry({
            "配置编号": "SAMP-9002",
            "数据源名称": "CATA-0001",
            "数据集编号": "CATA-0001",
            "采样算法": "随机抽样",
            "输出表名": "dwd_customer_sample",
        })
        self.assertIsNone(entry)
        self.assertTrue(any("输出表名" in error for error in errors))

    def test_stratified_field_must_exist_in_catalog(self) -> None:
        entry, errors = self.sample.create_entry({
            "配置编号": "SAMP-9003",
            "数据源名称": "CATA-0001",
            "数据集编号": "CATA-0001",
            "采样算法": "分层抽样",
            "分层字段": "不存在的字段",
            "输出表名": "tmp_sample_9003",
        })
        self.assertIsNone(entry)
        self.assertTrue(any("分层字段" in error for error in errors))


class FieldRuleTest(unittest.TestCase):
    def test_normalize_fields(self) -> None:
        self.assertEqual(
            normalize_fields(" 客户编号，客户姓名、客户编号\n注册时间 "),
            ["客户编号", "客户姓名", "注册时间"],
        )
        self.assertEqual(normalize_fields(None), [])

    def test_validate_fields_rejects_bad_input(self) -> None:
        fields, errors = validate_fields("")
        self.assertEqual(fields, [])
        self.assertTrue(errors)
        _, errors = validate_fields("字" * 65)
        self.assertTrue(errors)
        _, errors = validate_fields("bad\x00field")
        self.assertTrue(errors)

    def test_compute_level(self) -> None:
        self.assertEqual(compute_level(["商品名称"], "每月"), "L2")
        self.assertEqual(compute_level(["订单编号"], "每日"), "L3")
        self.assertEqual(compute_level(["手机号"], "每周"), "L4")
        self.assertEqual(compute_level([], "每日"), "L1")


if __name__ == "__main__":
    unittest.main()

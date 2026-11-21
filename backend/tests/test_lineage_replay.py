"""导出血缘查询与回溯重放测试。

覆盖验收链路：
搜索含两条已验证血缘和一处缺口的导出 → 查看返回范围 → 从检索结果发起重放
（中断/恢复/幂等）→ 补证缺口 → 对照实际读取的批次和原始行。
"""
from __future__ import annotations

from app.store import store
from app.services.lineage_manifest import (
    DEMO_BATCHES,
    DEMO_EXPORT_NO,
    DEMO_RULE,
    DEMO_VIEW,
    lineage_manifest_service,
)
from app.services.replay_runner import FINISHED, INTERRUPTED, replay_runner_service


def _reset_lineage_tables():
    # 去掉演示场景在业务表里写入的行，保证每个用例从干净状态构造。
    store.rows("lineage_manifest").clear()
    store.rows("replay_runs").clear()
    for module, field, values in [
        ("data_lineage", "view_name", (DEMO_VIEW,)),
        ("aggregation_view", "视图名称", (DEMO_VIEW,)),
        ("data_export", "导出编号", (DEMO_EXPORT_NO,)),
        ("ingest_batch", "批次编号", tuple(DEMO_BATCHES)),
        ("field_parser", "规则名称", (DEMO_RULE,)),
    ]:
        table = store.rows(module)
        table[:] = [
            r for r in table
            if not (
                r.get(field) in values
                or r.get("batch_no") in tuple(DEMO_BATCHES)
                or (field == "view_name" and r.get("batch_no") in DEMO_BATCHES)
            )
        ]
    store.rows("raw_records")[:] = [
        r for r in store.rows("raw_records") if r.get("批次编号") not in DEMO_BATCHES
    ]
    store.rows("source_registry")[:] = [
        r for r in store.rows("source_registry") if r.get("来源标识") != "lineage-demo-src"
    ]
    store.rows("replay_engine")[:] = [
        r for r in store.rows("replay_engine") if r.get("manifest_no")
    ]


def setup_demo():
    _reset_lineage_tables()
    lineage_manifest_service.bootstrap_demo_scenario()


class TestLineageSearch:
    def test_search_finds_demo_export_with_two_verified_and_one_gap(self):
        setup_demo()
        results = lineage_manifest_service.search_exports(DEMO_EXPORT_NO)
        assert len(results) == 1
        item = results[0]
        assert item["lineage_total"] == 3
        assert item["lineage_verified"] == 2
        assert item["lineage_gap"] == 1

    def test_search_keyword_matches_view_and_file_path(self):
        setup_demo()
        assert lineage_manifest_service.search_exports(DEMO_VIEW)
        assert lineage_manifest_service.search_exports(DEMO_EXPORT_NO)

    def test_bootstrap_is_idempotent(self):
        setup_demo()
        before = len(store.rows("data_lineage"))
        lineage_manifest_service.bootstrap_demo_scenario()
        assert len(store.rows("data_lineage")) == before


class TestLineageQueryScope:
    def test_query_returns_verified_scope_and_gap_separately(self):
        setup_demo()
        result = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        assert result["ok"] is True
        assert result["lineage_summary"] == {"total": 3, "verified": 2, "gap": 1}
        assert result["executable_scope"] == DEMO_BATCHES[:2]
        gap_batches = [g["batch_no"] for g in result["gaps"]]
        assert gap_batches == [DEMO_BATCHES[2]]
        assert "待验证" in result["gaps"][0]["reason"]
        # 缺口不得出现在可执行范围
        assert DEMO_BATCHES[2] not in result["executable_scope"]

    def test_query_includes_readable_logs_and_historical_pending_group_together(self):
        setup_demo()
        result = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        # 同一次检索同时给出当前可读来源与待核实历史分组
        assert len(result["readable_now"]) == 2
        assert len(result["pending_verification"]) == 1
        for item in result["readable_now"]:
            assert item["raw_count"] == 2
            assert len(item["raw_line_ids"]) == 2

    def test_query_compatible_with_historical_references(self):
        setup_demo()
        export = lineage_manifest_service.resolve_export(DEMO_EXPORT_NO)
        # 历史导出可能只留下 ID、export-{id} 下游标识、视图名或文件路径
        for ref in (
            str(export["id"]),
            f"export-{export['id']}",
            export["导出范围"],
            export["文件路径"],
        ):
            found = lineage_manifest_service.resolve_export(ref)
            assert found is not None and found["id"] == export["id"]

    def test_unknown_export_ref_rejected(self):
        setup_demo()
        result = lineage_manifest_service.query_export_lineage("NO-SUCH-EXPORT")
        assert result["ok"] is False

    def test_manifest_persisted_and_refreshable(self):
        setup_demo()
        first = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        stored = lineage_manifest_service.get_manifest(first["manifest_no"])
        assert stored is not None
        assert stored["executable_scope"] == DEMO_BATCHES[:2]
        # 再次查询刷新同一份清单而不是新建
        second = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        assert second["manifest_no"] == first["manifest_no"]
        assert len(store.rows("lineage_manifest")) == 1

    def test_missing_batch_and_missing_raw_become_gaps(self):
        setup_demo()
        # 把 BATCH-L2 的原文删掉、把 BATCH-L1 从批次表移除，核实仍应判为缺口
        store.rows("raw_records")[:] = [
            r for r in store.rows("raw_records") if r.get("批次编号") != DEMO_BATCHES[1]
        ]
        store.rows("ingest_batch")[:] = [
            b for b in store.rows("ingest_batch") if b.get("批次编号") != DEMO_BATCHES[0]
        ]
        result = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        gap_map = {g["batch_no"]: g["reason"] for g in result["gaps"]}
        assert "原始日志缺失" in gap_map[DEMO_BATCHES[1]]
        assert "批次表中不存在" in gap_map[DEMO_BATCHES[0]]
        assert result["executable_scope"] == []


class TestReplayFromManifest:
    def test_replay_reads_only_verified_scope_and_writes_back_source_list(self):
        setup_demo()
        query = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        result = replay_runner_service.start_replay(query["manifest_no"], DEMO_RULE)
        assert result["ok"] is True
        assert result["finished"] is True
        run = result["run"]
        assert run["processed_batches"] == DEMO_BATCHES[:2]
        assert run["status"] == FINISHED
        assert len(run["read_raw_ids"]) == 4  # 2 批次 × 2 行

        export = lineage_manifest_service.resolve_export(DEMO_EXPORT_NO)
        import json
        source_list = json.loads(export["来源清单"])
        replayed = [s for s in source_list if s["replay_status"] == "已重放"]
        excluded = [s for s in source_list if s["replay_status"] == "未核实-未进入重放"]
        assert sorted(s["batch_no"] for s in replayed) == DEMO_BATCHES[:2]
        assert excluded and excluded[0]["batch_no"] == DEMO_BATCHES[2]

    def test_replay_refuses_when_no_verified_scope(self):
        setup_demo()
        # 把全部血缘改成待验证 → 清单无可执行范围
        for ln in store.rows("data_lineage"):
            ln["status"] = "待验证"
        query = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        result = replay_runner_service.start_replay(query["manifest_no"], DEMO_RULE)
        assert result["ok"] is False
        assert "不得加入可执行范围" in result["message"]

    def test_interrupted_replay_resumes_from_last_read_batch(self):
        setup_demo()
        query = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        first = replay_runner_service.start_replay(
            query["manifest_no"], DEMO_RULE, max_batches=1
        )
        run_id = first["run"]["id"]
        assert first["finished"] is False
        assert first["run"]["status"] == INTERRUPTED
        assert first["run"]["last_read_batch"] == DEMO_BATCHES[0]

        # 恢复时能定位上次实际读取的批次，从其后继续
        resumed = replay_runner_service.resume_replay(run_id)
        assert resumed["ok"] is True
        assert resumed["run"]["processed_batches"] == DEMO_BATCHES[:2]
        assert resumed["run"]["last_read_batch"] == DEMO_BATCHES[1]
        assert resumed["run"]["status"] == FINISHED

    def test_resume_and_resubmit_are_idempotent_single_result_per_batch(self):
        setup_demo()
        query = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        first = replay_runner_service.start_replay(
            query["manifest_no"], DEMO_RULE, max_batches=1
        )
        run_id = first["run"]["id"]
        # 中断后再次提交同一批：已读批次不得重复记账
        again = replay_runner_service.start_replay(query["manifest_no"], DEMO_RULE)
        assert again["run"]["id"] == run_id
        assert again["run"]["read_raw_ids"].count(first["run"]["read_raw_ids"][0]) == 1
        replay_runner_service.resume_replay(run_id)
        # 完成后重复提交只返回既有结果
        final = replay_runner_service.start_replay(query["manifest_no"], DEMO_RULE)
        assert final["run"]["id"] == run_id
        assert len(final["run"]["read_raw_ids"]) == 4
        # 每条原文只产出一条修正明细
        replay_outputs = [
            p for p in store.rows("parsed_entity")
            if str(p.get("replay_run_id")) == str(run_id)
        ]
        assert len(replay_outputs) == 4
        raw_ids = [int(p["原始记录ID"]) for p in replay_outputs]
        assert len(raw_ids) == len(set(raw_ids))


class TestVerifyThenReconcile:
    def test_verify_gap_then_replay_includes_it_and_reconcile_matches_raw_lines(self):
        setup_demo()
        query = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        gap_lineage = query["gaps"][0]
        verify = lineage_manifest_service.verify_lineage(lineage_id=gap_lineage["lineage_id"])
        assert verify["ok"] is True
        assert verify["lineage"]["status"] == "已记录"

        # 补证后重新查询，缺口转入可执行范围
        refreshed = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        assert refreshed["lineage_summary"]["gap"] == 0
        assert refreshed["executable_scope"] == DEMO_BATCHES

        replay_runner_service.start_replay(refreshed["manifest_no"], DEMO_RULE)
        check = replay_runner_service.reconcile(DEMO_EXPORT_NO)
        assert check["ok"] is True
        assert check["all_numbers_accounted"] is True
        assert check["actually_read_batches"] == DEMO_BATCHES
        assert check["excluded_gaps"] == []
        # 对照原始行：每批 2 行，共 6 条原文全部有产出
        total_raw = sum(len(item["raw_line_ids"]) for item in check["read_raw_lines"])
        total_output = sum(len(item["output_ids"]) for item in check["read_raw_lines"])
        assert total_raw == 6 and total_output == 6

    def test_verify_by_export_ref_and_batch_no(self):
        setup_demo()
        result = lineage_manifest_service.verify_lineage(
            export_ref=DEMO_EXPORT_NO, batch_no=DEMO_BATCHES[2]
        )
        assert result["ok"] is True
        assert result["lineage"]["status"] == "已记录"

    def test_reconcile_before_full_replay_reports_unaccounted(self):
        setup_demo()
        query = lineage_manifest_service.query_export_lineage(DEMO_EXPORT_NO)
        replay_runner_service.start_replay(
            query["manifest_no"], DEMO_RULE, max_batches=1
        )
        check = replay_runner_service.reconcile(DEMO_EXPORT_NO)
        assert check["all_numbers_accounted"] is False
        assert check["actually_read_batches"] == [DEMO_BATCHES[0]]
        assert check["excluded_gaps"][0]["batch_no"] == DEMO_BATCHES[2]

"""回溯重放执行器：按可重放批次清单读取原文，产出修正明细并回写交付物来源清单。

关键约束：
- 只按 lineage_manifest.executable_scope 读取批次；没核实的来源（缺口）永不进入执行范围；
- 每读完一个批次就落检查点 last_read_batch，中断后恢复从「上次实际读取的批次」之后继续；
- 同一批重复提交按 manifest+规则 幂等：已读批次直接跳过，每条原文只产出一次结果；
- 执行结果回写 data_export.来源清单，逐编号登记实际读取的批次、原始行与产出明细，
  值班人员可对照每个进入重放的编号核对。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.lineage_manifest import (
    LineageManifestService,
    _dump,
    _load,
    _next_id,
    lineage_manifest_service,
)

REPLAY_TABLE = "replay_runs"
FINISHED = "已完成"
INTERRUPTED = "已中断"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class ReplayRunnerService:
    def __init__(self, manifest_service: LineageManifestService) -> None:
        self.manifests = manifest_service

    # ------------------------------------------------------------------
    def _find_run(self, manifest_no: str, rule: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows(REPLAY_TABLE)
             if r.get("manifest_no") == manifest_no and r.get("rule") == rule),
            None,
        )

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = next((r for r in store.rows(REPLAY_TABLE) if int(r.get("id", 0)) == run_id), None)
        return self._hydrate(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        return [self._hydrate(r) for r in store.rows(REPLAY_TABLE)]

    def _hydrate(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "processed_batches": _load(row.get("processed_batches"), []),
            "read_raw_ids": _load(row.get("read_raw_ids"), []),
            "outputs": _load(row.get("outputs"), []),
            "scope": _load(row.get("scope"), []),
        }

    def _parse_body(self, body: str, rule: dict[str, Any] | None) -> dict[str, Any]:
        parts = body.split("|") if "|" in body else [body]
        raw_field_list = ((rule or {}).get("输出字段列表", "") or "").strip()
        if raw_field_list and "," in raw_field_list:
            field_names = [f.strip() for f in raw_field_list.split(",")]
        else:
            field_names = ["time", "level", "message"]
        return {
            fn: (parts[i].strip() if i < len(parts) else "")
            for i, fn in enumerate(field_names) if fn
        }

    # ------------------------------------------------------------------
    def start_replay(
        self,
        manifest_no: str,
        rule: str,
        *,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """从检索结果发起重放；同一 manifest+规则重复提交走幂等/续跑路径。"""
        manifest = self.manifests.get_manifest(manifest_no)
        if manifest is None:
            return {"ok": False, "message": f"清单 {manifest_no} 不存在，请先发起导出血缘查询"}

        scope: list[str] = list(manifest["executable_scope"])
        if not scope:
            return {
                "ok": False,
                "message": "清单内没有已核实来源，未核实来源不得加入可执行范围",
                "manifest_no": manifest_no,
            }

        run = self._find_run(manifest_no, rule)
        resumed = run is not None
        if run is None:
            run = {
                "id": _next_id(REPLAY_TABLE),
                "manifest_no": manifest_no,
                "export_id": manifest["export_id"],
                "rule": rule,
                "scope": _dump(scope),
                "status": "重放中",
                "processed_batches": _dump([]),
                "last_read_batch": "",
                "read_raw_ids": _dump([]),
                "outputs": _dump([]),
                "started_at": _now_iso(),
                "finished_at": "",
            }
            store.rows(REPLAY_TABLE).append(run)
            # 同步在回溯重放引擎模块登记一条任务，保持工作台可见。
            store.rows("replay_engine").append({
                "id": _next_id("replay_engine"),
                "历史批次范围": ",".join(scope),
                "新解析规则版本": rule,
                "重放状态": "重放中",
                "产出记录数": "0",
                "执行时间": _now_iso(),
                "status": "重放中",
                "pending": True,
                "abnormal": False,
                "manifest_no": manifest_no,
                "replay_run_id": str(run["id"]),
            })

        result = self._advance(run, max_batches=max_batches)
        result["resumed"] = resumed
        return result

    def resume_replay(self, run_id: int, *, max_batches: int | None = None) -> dict[str, Any]:
        """中断恢复：定位上次实际读取的批次，从其后继续。"""
        run = next(
            (r for r in store.rows(REPLAY_TABLE) if int(r.get("id", 0)) == run_id),
            None,
        )
        if run is None:
            return {"ok": False, "message": f"重放任务 {run_id} 不存在"}
        if run.get("status") == FINISHED:
            return {
                "ok": True,
                "message": "重放已完成，重复提交只返回既有结果，不重复记账",
                "run": self._hydrate(run),
                "resumed": True,
            }
        return self._advance(run, max_batches=max_batches, resumed=True)

    # ------------------------------------------------------------------
    def _advance(
        self,
        run: dict[str, Any],
        *,
        max_batches: int | None,
        resumed: bool = False,
    ) -> dict[str, Any]:
        manifest = self.manifests.get_manifest(str(run["manifest_no"]))
        scope: list[str] = list(manifest["executable_scope"])
        processed: list[str] = _load(run.get("processed_batches"), [])
        read_raw_ids: list[int] = _load(run.get("read_raw_ids"), [])
        outputs: list[dict[str, Any]] = _load(run.get("outputs"), [])

        rule_row = next(
            (p for p in store.rows("field_parser") if p.get("规则名称") == run["rule"]),
            None,
        )
        pending = [b for b in scope if b not in processed]
        run["status"] = "重放中"
        read_this_round = 0

        for batch_no in pending:
            if max_batches is not None and read_this_round >= max_batches:
                break
            raw_rows = sorted(
                (r for r in store.rows("raw_records") if r.get("批次编号") == batch_no),
                key=lambda r: int(r.get("id", 0)),
            )
            batch_outputs: list[dict[str, Any]] = []
            for raw in raw_rows:
                raw_id = int(raw["id"])
                # 幂等：同一条原文在同一重放里只读取、只产出一次。
                if raw_id in read_raw_ids:
                    continue
                fields = self._parse_body(str(raw.get("原始正文", "")), rule_row)
                output_id = _next_id("parsed_entity")
                store.rows("parsed_entity").append({
                    "id": output_id,
                    "来源批次": batch_no,
                    "原始记录ID": str(raw_id),
                    "解析规则版本": run["rule"],
                    "标准化字段集": _dump(fields),
                    "校验状态": "",
                    "status": "待校验",
                    "pending": True,
                    "abnormal": False,
                    "replay_run_id": str(run["id"]),
                    "manifest_no": run["manifest_no"],
                    "replayed_at": _now_iso(),
                    "origin_line_no": raw.get("原始行号"),
                })
                read_raw_ids.append(raw_id)
                batch_outputs.append({
                    "raw_id": raw_id,
                    "原始行号": raw.get("原始行号"),
                    "output_id": output_id,
                })

            processed.append(batch_no)
            run["last_read_batch"] = batch_no  # 检查点：上次实际读取的批次
            outputs.append({
                "batch_no": batch_no,
                "read_at": _now_iso(),
                "raw_count": len(raw_rows),
                "raw_line_ids": [int(r["id"]) for r in raw_rows],
                "outputs": batch_outputs,
            })
            read_this_round += 1

        finished = all(b in processed for b in scope)
        run["processed_batches"] = _dump(processed)
        run["read_raw_ids"] = _dump(read_raw_ids)
        run["outputs"] = _dump(outputs)
        run["scope"] = _dump(scope)
        if finished:
            run["status"] = FINISHED
            run["finished_at"] = _now_iso()
        else:
            run["status"] = INTERRUPTED

        # 同步回溯重放引擎模块任务状态。
        for engine_row in reversed(store.rows("replay_engine")):
            if engine_row.get("replay_run_id") == str(run["id"]):
                engine_row["status"] = run["status"]
                engine_row["重放状态"] = run["status"]
                engine_row["产出记录数"] = str(len(read_raw_ids))
                engine_row["pending"] = not finished
                break

        self._write_back_export(run, manifest, outputs)
        message = (
            f"重放完成，实际读取 {len(processed)}/{len(scope)} 个批次、{len(read_raw_ids)} 条原文"
            if finished else
            f"重放中断于检查点 {run['last_read_batch']}，已读 {len(processed)}/{len(scope)} 个批次，可恢复续跑"
        )
        return {
            "ok": True,
            "message": message,
            "resumed": resumed,
            "run": self._hydrate(run),
            "finished": finished,
        }

    def _write_back_export(
        self,
        run: dict[str, Any],
        manifest: dict[str, Any],
        outputs: list[dict[str, Any]],
    ) -> None:
        """把每个进入重放的编号回写到交付物的来源清单，供逐编号核对。"""
        read_by_batch = {item["batch_no"]: item for item in outputs}
        source_list: list[dict[str, Any]] = []

        for item in manifest["verified"]:
            batch_no = item["batch_no"]
            read = read_by_batch.get(batch_no)
            source_list.append({
                "batch_no": batch_no,
                "lineage_status": item["lineage_status"],
                "raw_count": item["raw_count"],
                "raw_line_ids": item["raw_line_ids"],
                "replay_status": "已重放" if read else "待重放",
                "replay_run_id": run["id"] if read else None,
                "read_at": read["read_at"] if read else None,
                "output_ids": [o["output_id"] for o in read["outputs"]] if read else [],
            })
        # 缺口同时登记在来源清单里，明确「未核实、未进入可执行范围」。
        for item in manifest["gaps"]:
            source_list.append({
                "batch_no": item["batch_no"],
                "lineage_status": item["lineage_status"],
                "raw_count": item["raw_count"],
                "raw_line_ids": item["raw_line_ids"],
                "replay_status": "未核实-未进入重放",
                "replay_run_id": None,
                "read_at": None,
                "output_ids": [],
                "gap_reason": item["reason"],
            })

        export = store.find("data_export", int(manifest["export_id"]))
        if export is not None:
            export["来源清单"] = _dump(source_list)
            export["replay_manifest_no"] = manifest["manifest_no"]
            export["replay_run_id"] = str(run["id"])
            export["last_replay_at"] = _now_iso()

    def reconcile(self, export_ref: str) -> dict[str, Any]:
        """补证后对照：返回交付物实际读取批次、原始行与清单范围的核对结果。"""
        export = self.manifests.resolve_export(export_ref)
        if export is None:
            return {"ok": False, "message": f"未找到导出引用：{export_ref}"}
        source_list = _load(export.get("来源清单"), [])
        read_batches = [s["batch_no"] for s in source_list if s.get("replay_status") == "已重放"]
        planned_runs = [
            self._hydrate(r) for r in store.rows(REPLAY_TABLE)
            if int(r.get("export_id", 0)) == int(export["id"])
        ]
        scope = planned_runs[-1]["scope"] if planned_runs else []
        return {
            "ok": True,
            "export_id": export["id"],
            "导出编号": export.get("导出编号"),
            "manifest_scope": scope,
            "actually_read_batches": read_batches,
            "read_raw_lines": [
                {
                    "batch_no": s["batch_no"],
                    "raw_line_ids": s.get("raw_line_ids", []),
                    "output_ids": s.get("output_ids", []),
                }
                for s in source_list if s.get("replay_status") == "已重放"
            ],
            "excluded_gaps": [
                {"batch_no": s["batch_no"], "reason": s.get("gap_reason")}
                for s in source_list if s.get("replay_status") == "未核实-未进入重放"
            ],
            "all_numbers_accounted": sorted(scope) == sorted(read_batches)
            and not any(s.get("replay_status") == "待重放" for s in source_list),
            "source_list": source_list,
        }


replay_runner_service = ReplayRunnerService(lineage_manifest_service)

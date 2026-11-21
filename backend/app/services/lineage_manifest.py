"""导出血缘查询：按交付物（导出任务）解析视图分组关联的来源血缘，生成可重放批次清单。

业务规则：
- 只有「血缘已记录 + 来源批次存在 + 原始日志当前可读」三者同时满足的来源，
  才能进入可执行范围（executable_scope）；
- 血缘待验证/验证异常、批次缺失、原文缺失的来源统一列为缺口（gaps），
  未核实来源不得加入重放；
- 同一次检索可能同时定位到当前就能读取的日志和仍需核实的历史分组，两类都返回，
  但只有前者可重放；
- 清单快照持久化到 lineage_manifest 表，重放引擎按清单编号读取，保证可重放、可核对。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.store import store

MANIFEST_TABLE = "lineage_manifest"

# 演示场景固定标识：一份含两条已验证血缘和一处缺口的导出。
DEMO_EXPORT_NO = "DL-EXPORT-01"
DEMO_VIEW = "视图-交付物DL-01"
DEMO_BATCHES = ["BATCH-L1", "BATCH-L2", "BATCH-L3"]
DEMO_SOURCE = "lineage-demo-src"
DEMO_RULE = "dl-parse-rule-v2"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _next_id(module: str) -> int:
    return max((int(r.get("id", 0)) for r in store.rows(module)), default=0) + 1


class LineageManifestService:
    """导出血缘查询与可重放批次清单生成。"""

    # ------------------------------------------------------------------
    # 演示场景：两条已验证血缘（BATCH-L1/L2）+ 一处缺口（BATCH-L3 待验证）
    # ------------------------------------------------------------------
    def bootstrap_demo_scenario(self) -> dict[str, Any]:
        """幂等构造演示导出与血缘场景，已存在时直接返回既有导出。"""
        exports = store.rows("data_export")
        for row in exports:
            if row.get("导出编号") == DEMO_EXPORT_NO:
                return {"ok": True, "message": "演示场景已存在", "export": row}

        # 数据源与三条来源批次，每条批次带两条原始日志（当前均可读）。
        sources = store.rows("source_registry")
        if not any(s.get("来源标识") == DEMO_SOURCE for s in sources):
            sources.append({
                "id": _next_id("source_registry"),
                "来源标识": DEMO_SOURCE,
                "接入协议": "http",
                "连接凭证": "token-demo-lineage",
                "采集区域": "default",
                "状态": "已验证",
                "status": "已验证",
                "pending": False,
                "abnormal": False,
            })

        batches = store.rows("ingest_batch")
        raw_table = store.rows("raw_records")
        for idx, batch_no in enumerate(DEMO_BATCHES, start=1):
            batches.append({
                "id": _next_id("ingest_batch"),
                "批次编号": batch_no,
                "所属数据源": DEMO_SOURCE,
                "采集时间窗口": f"2026-Q3-W{idx}",
                "记录总数": "2",
                "批次状态": "已完成",
                "status": "已完成",
                "pending": False,
                "abnormal": False,
            })
            for line_no in (1, 2):
                raw_table.append({
                    "id": _next_id("raw_records"),
                    "批次编号": batch_no,
                    "原始行号": str(line_no),
                    "采集时间戳": f"2026-09-0{idx}T10:0{line_no}:00",
                    "原始正文": f"2026-09-0{idx}T10:0{line_no}:00|INFO|{batch_no}-line{line_no}",
                    "解析状态": "已解析",
                    "status": "已解析",
                    "pending": False,
                    "abnormal": False,
                })

        # 解析规则（重放时使用的新版本）。
        parsers = store.rows("field_parser")
        if not any(p.get("规则名称") == DEMO_RULE for p in parsers):
            parsers.append({
                "id": _next_id("field_parser"),
                "规则名称": DEMO_RULE,
                "适用数据源": DEMO_SOURCE,
                "输入格式": "pipe-log",
                "输出字段列表": "time,level,message",
                "解析引擎": "builtin",
                "status": "已发布",
                "pending": False,
                "abnormal": False,
            })

        # 视图分组：交付物挂在一个已生效聚合视图下。
        views = store.rows("aggregation_view")
        if not any(v.get("视图名称") == DEMO_VIEW for v in views):
            views.append({
                "id": _next_id("aggregation_view"),
                "视图名称": DEMO_VIEW,
                "聚合维度": "level",
                "指标定义": "count(message)",
                "数据源范围": ",".join(DEMO_BATCHES),
                "刷新间隔": "manual",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            })

        export_id = _next_id("data_export")
        export_entry = {
            "id": export_id,
            "导出编号": DEMO_EXPORT_NO,
            "导出范围": DEMO_VIEW,
            "目标格式": "csv",
            "过滤条件": "{}",
            "导出状态": "已完成",
            "文件路径": f"/exports/archive/{DEMO_EXPORT_NO}.csv",
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "来源清单": _dump([]),
        }
        exports.append(export_entry)

        # 血缘关系：前两条已记录（可重放），第三条待验证（缺口）。
        lineages = store.rows("data_lineage")
        lineage_status = ["已记录", "已记录", "待验证"]
        for idx, batch_no in enumerate(DEMO_BATCHES):
            lineages.append({
                "id": _next_id("data_lineage"),
                "来源批次": batch_no,
                "解析规则版本": DEMO_RULE,
                "标准化版本": "schema-v1",
                "下游消费标识": f"export-{export_id}",
                "记录时间": _now_iso(),
                "status": lineage_status[idx],
                "pending": lineage_status[idx] != "已记录",
                "abnormal": lineage_status[idx] == "验证异常",
                "export_id": str(export_id),
                "view_name": DEMO_VIEW,
                "batch_no": batch_no,
            })
        return {"ok": True, "message": "演示场景已初始化", "export": export_entry}

    # ------------------------------------------------------------------
    # 导出检索：兼容历史导出引用（ID / 导出编号 / 视图名 / 文件路径）
    # ------------------------------------------------------------------
    def resolve_export(self, export_ref: str) -> dict[str, Any] | None:
        ref = export_ref.strip()
        if not ref:
            return None
        exports = store.rows("data_export")
        # 1) 主键 ID
        if ref.isdigit():
            by_id = next((e for e in exports if int(e.get("id", 0)) == int(ref)), None)
            if by_id:
                return by_id
        # 2) 导出编号 / 下游消费标识（export-12 这类历史引用）
        for row in exports:
            if row.get("导出编号") == ref:
                return row
        # 3) 视图名（导出范围）与文件路径：历史导出常只留这两类引用
        for row in exports:
            if row.get("导出范围") == ref or row.get("文件路径") == ref:
                return row
        suffix = ref.replace("export-", "")
        if suffix.isdigit():
            return next(
                (e for e in exports if int(e.get("id", 0)) == int(suffix)), None
            )
        return None

    def search_exports(self, keyword: str | None = None) -> list[dict[str, Any]]:
        """检索导出交付物，并附带血缘核实情况摘要。"""
        self.bootstrap_demo_scenario()
        exports = list(store.rows("data_export"))
        if keyword:
            kw = keyword.strip().casefold()
            exports = [
                e for e in exports
                if kw in str(e.get("导出编号", "")).casefold()
                or kw in str(e.get("导出范围", "")).casefold()
                or kw in str(e.get("文件路径", "")).casefold()
            ]
        lineages = store.rows("data_lineage")
        result: list[dict[str, Any]] = []
        for export in exports:
            related = [
                ln for ln in lineages
                if str(ln.get("export_id", "")) == str(export.get("id"))
                or ln.get("下游消费标识") == f"export-{export.get('id')}"
            ]
            verified = sum(1 for ln in related if ln.get("status") == "已记录")
            gap = len(related) - verified
            result.append({
                "id": export.get("id"),
                "导出编号": export.get("导出编号", f"export-{export.get('id')}"),
                "导出范围": export.get("导出范围"),
                "文件路径": export.get("文件路径"),
                "status": export.get("status"),
                "lineage_total": len(related),
                "lineage_verified": verified,
                "lineage_gap": gap,
            })
        return result

    # ------------------------------------------------------------------
    # 血缘查询 → 可重放批次清单
    # ------------------------------------------------------------------
    def _batch_readable_raw(self, batch_no: str) -> list[dict[str, Any]]:
        return sorted(
            (r for r in store.rows("raw_records") if r.get("批次编号") == batch_no),
            key=lambda r: int(r.get("id", 0)),
        )

    def query_export_lineage(self, export_ref: str) -> dict[str, Any]:
        """读取导出关联的视图分组与来源血缘，生成（或刷新）可重放批次清单。"""
        self.bootstrap_demo_scenario()
        export = self.resolve_export(export_ref)
        if export is None:
            return {"ok": False, "message": f"未找到导出引用：{export_ref}"}

        export_id = int(export["id"])
        lineages = [
            ln for ln in store.rows("data_lineage")
            if str(ln.get("export_id", "")) == str(export_id)
            or ln.get("下游消费标识") == f"export-{export_id}"
        ]
        view_names = sorted({
            str(ln.get("view_name") or export.get("导出范围")) for ln in lineages
        })
        if export.get("导出范围") and export["导出范围"] not in view_names:
            view_names.append(str(export["导出范围"]))
        view_groups = [
            {
                "view_name": name,
                "status": next(
                    (v.get("status") for v in store.rows("aggregation_view")
                     if v.get("视图名称") == name),
                    "未登记",
                ),
            }
            for name in view_names
        ]

        batches = {b.get("批次编号"): b for b in store.rows("ingest_batch")}
        verified: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []

        # 以血缘关系为主线读取每个视图分组关联的来源批次。
        for ln in sorted(lineages, key=lambda r: int(r.get("id", 0))):
            batch_no = str(ln.get("batch_no") or ln.get("来源批次"))
            lineage_status = ln.get("status")
            raw_rows = self._batch_readable_raw(batch_no)
            reasons: list[str] = []
            if lineage_status != "已记录":
                reasons.append(
                    "血缘待验证，来源尚未核实" if lineage_status == "待验证"
                    else f"血缘{lineage_status}"
                )
            if batch_no not in batches:
                reasons.append("来源批次在批次表中不存在")
            if not raw_rows:
                reasons.append("原始日志缺失或当前不可读")

            entry = {
                "batch_no": batch_no,
                "view_name": ln.get("view_name") or export.get("导出范围"),
                "lineage_id": ln.get("id"),
                "lineage_status": lineage_status,
                "raw_count": len(raw_rows),
                "raw_line_ids": [r.get("id") for r in raw_rows],
            }
            if reasons:
                gaps.append({**entry, "reason": "；".join(reasons)})
            else:
                verified.append(entry)

        executable_scope = [item["batch_no"] for item in verified]
        manifest_no = f"MAN-{export_id:04d}"
        manifest_row = {
            "manifest_no": manifest_no,
            "export_id": export_id,
            "export_ref": export_ref,
            "view_groups": _dump(view_groups),
            "verified": _dump(verified),
            "gaps": _dump(gaps),
            "executable_scope": _dump(executable_scope),
            "status": "可重放" if verified else "待核实",
            "updated_at": _now_iso(),
        }
        table = store.rows(MANIFEST_TABLE)
        existing = next((m for m in table if m.get("manifest_no") == manifest_no), None)
        if existing is None:
            manifest_row["id"] = _next_id(MANIFEST_TABLE)
            manifest_row["created_at"] = _now_iso()
            table.append(manifest_row)
        else:
            existing.update(manifest_row)

        return {
            "ok": True,
            "manifest_no": manifest_no,
            "export": {
                "id": export_id,
                "导出编号": export.get("导出编号", f"export-{export_id}"),
                "导出范围": export.get("导出范围"),
                "文件路径": export.get("文件路径"),
                "status": export.get("status"),
            },
            "view_groups": view_groups,
            "lineage_summary": {
                "total": len(lineages),
                "verified": len(verified),
                "gap": len(gaps),
            },
            "readable_now": verified,
            "pending_verification": gaps,
            "verified": verified,
            "gaps": gaps,
            "executable_scope": executable_scope,
        }

    def get_manifest(self, manifest_no: str) -> dict[str, Any] | None:
        row = next(
            (m for m in store.rows(MANIFEST_TABLE) if m.get("manifest_no") == manifest_no),
            None,
        )
        if row is None:
            return None
        return {
            **row,
            "view_groups": _load(row.get("view_groups"), []),
            "verified": _load(row.get("verified"), []),
            "gaps": _load(row.get("gaps"), []),
            "executable_scope": _load(row.get("executable_scope"), []),
        }

    # ------------------------------------------------------------------
    # 补证：把待验证/异常血缘核实为已记录（对齐 data_lineage 的「验证血缘」动作）
    # ------------------------------------------------------------------
    def verify_lineage(
        self,
        *,
        lineage_id: int | None = None,
        export_ref: str | None = None,
        batch_no: str | None = None,
    ) -> dict[str, Any]:
        self.bootstrap_demo_scenario()
        lineages = store.rows("data_lineage")
        target = None
        if lineage_id is not None:
            target = next((ln for ln in lineages if int(ln.get("id", 0)) == lineage_id), None)
        elif export_ref and batch_no:
            export = self.resolve_export(export_ref)
            if export is None:
                return {"ok": False, "message": f"未找到导出引用：{export_ref}"}
            target = next(
                (ln for ln in lineages
                 if (str(ln.get("export_id", "")) == str(export["id"])
                     or ln.get("下游消费标识") == f"export-{export['id']}")
                 and str(ln.get("batch_no") or ln.get("来源批次")) == batch_no),
                None,
            )
        if target is None:
            return {"ok": False, "message": "未找到需要补证的血缘关系"}
        if target.get("status") == "已记录":
            return {"ok": True, "message": "血缘已核实，无需重复补证", "lineage": target}
        target["status"] = "已记录"
        target["pending"] = False
        target["abnormal"] = False
        target["verified_at"] = _now_iso()
        return {"ok": True, "message": f"血缘 {target.get('id')} 已补证核实", "lineage": target}


lineage_manifest_service = LineageManifestService()

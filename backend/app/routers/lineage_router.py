"""导出血缘查询与回溯重放路由。

面向值班场景：先按导出交付物检索血缘、查看可重放范围与缺口，补证后从检索结果
发起重放；中断可按检查点续跑，结果回写交付物来源清单并支持逐编号核对。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.lineage_manifest import lineage_manifest_service
from app.services.replay_runner import replay_runner_service

router = APIRouter(prefix="/api/lineage", tags=["导出血缘与重放"])


class LineageQueryPayload(BaseModel):
    export_ref: str = Field(..., min_length=1, description="导出ID/导出编号/视图名/文件路径等历史引用")


class LineageVerifyPayload(BaseModel):
    lineage_id: int | None = None
    export_ref: str | None = None
    batch_no: str | None = None


class ReplayStartPayload(BaseModel):
    manifest_no: str = Field(..., min_length=1)
    rule: str = Field(..., min_length=1)
    max_batches: int | None = Field(default=None, ge=1, description="本次最多读取批次数，用于模拟中断")


class ReplayResumePayload(BaseModel):
    run_id: int
    max_batches: int | None = Field(default=None, ge=1)


@router.get("/exports")
def search_exports(keyword: str | None = None) -> dict[str, Any]:
    items = lineage_manifest_service.search_exports(keyword)
    return {"ok": True, "total": len(items), "items": items}


@router.post("/query")
def query_export_lineage(payload: LineageQueryPayload) -> dict[str, Any]:
    return lineage_manifest_service.query_export_lineage(payload.export_ref)


@router.post("/verify")
def verify_lineage(payload: LineageVerifyPayload) -> dict[str, Any]:
    return lineage_manifest_service.verify_lineage(
        lineage_id=payload.lineage_id,
        export_ref=payload.export_ref,
        batch_no=payload.batch_no,
    )


@router.post("/replay/start")
def start_replay(payload: ReplayStartPayload) -> dict[str, Any]:
    return replay_runner_service.start_replay(
        payload.manifest_no, payload.rule, max_batches=payload.max_batches
    )


@router.post("/replay/resume")
def resume_replay(payload: ReplayResumePayload) -> dict[str, Any]:
    return replay_runner_service.resume_replay(payload.run_id, max_batches=payload.max_batches)


@router.get("/replay/runs")
def list_replay_runs() -> dict[str, Any]:
    return {"ok": True, "items": replay_runner_service.list_runs()}


@router.get("/replay/runs/{run_id}")
def get_replay_run(run_id: int) -> dict[str, Any]:
    run = replay_runner_service.get_run(run_id)
    if run is None:
        return {"ok": False, "message": f"重放任务 {run_id} 不存在"}
    return {"ok": True, "run": run}


@router.get("/reconcile")
def reconcile(export_ref: str) -> dict[str, Any]:
    return replay_runner_service.reconcile(export_ref)

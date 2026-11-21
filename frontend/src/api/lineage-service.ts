import { request } from './client'

export type LineageSearchItem = {
  id: number
  导出编号: string
  导出范围: string
  文件路径: string
  status: string
  lineage_total: number
  lineage_verified: number
  lineage_gap: number
}

export type LineageEntry = {
  batch_no: string
  view_name: string
  lineage_id: number
  lineage_status: string
  raw_count: number
  raw_line_ids: number[]
  reason?: string
}

export type LineageQueryResult = {
  ok: boolean
  message?: string
  manifest_no: string
  export: { id: number; 导出编号: string; 导出范围: string; 文件路径: string; status: string }
  view_groups: { view_name: string; status: string }[]
  lineage_summary: { total: number; verified: number; gap: number }
  readable_now: LineageEntry[]
  pending_verification: LineageEntry[]
  verified: LineageEntry[]
  gaps: LineageEntry[]
  executable_scope: string[]
}

export type ReplayRun = {
  id: number
  manifest_no: string
  rule: string
  status: string
  scope: string[]
  processed_batches: string[]
  last_read_batch: string
  read_raw_ids: number[]
  outputs: { batch_no: string; raw_count: number; raw_line_ids: number[] }[]
  started_at: string
  finished_at: string
}

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return await response.json()
}

export async function searchLineageExports(keyword: string): Promise<LineageSearchItem[]> {
  const query = keyword ? `?keyword=${encodeURIComponent(keyword)}` : ''
  const response = await request(`/api/lineage/exports${query}`)
  if (!response.ok) throw new Error('导出检索失败')
  const payload = await response.json()
  return payload.items
}

export async function queryExportLineage(exportRef: string): Promise<LineageQueryResult> {
  return postJson('/api/lineage/query', { export_ref: exportRef })
}

export async function verifyLineage(lineageId: number): Promise<{ ok: boolean; message: string }> {
  return postJson('/api/lineage/verify', { lineage_id: lineageId })
}

export async function startReplay(
  manifestNo: string,
  rule: string,
  maxBatches?: number,
): Promise<{ ok: boolean; message: string; finished: boolean; run: ReplayRun }> {
  return postJson('/api/lineage/replay/start', {
    manifest_no: manifestNo,
    rule,
    ...(maxBatches ? { max_batches: maxBatches } : {}),
  })
}

export async function resumeReplay(
  runId: number,
): Promise<{ ok: boolean; message: string; run: ReplayRun }> {
  return postJson('/api/lineage/replay/resume', { run_id: runId })
}

export async function reconcileExport(
  exportRef: string,
): Promise<Record<string, unknown>> {
  const response = await request(`/api/lineage/reconcile?export_ref=${encodeURIComponent(exportRef)}`)
  if (!response.ok) throw new Error('来源核对失败')
  return await response.json()
}

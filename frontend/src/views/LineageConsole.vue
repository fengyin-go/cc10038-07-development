<template>
  <section class="page lineage-page">
    <header class="page-head">
      <div>
        <h2>导出血缘查询与回溯重放</h2>
        <p class="page-desc">
          按交付物检索视图分组的来源血缘，生成可重放批次清单；未核实来源不进可执行范围，
          重放结果回写交付物来源清单，支持断点续跑与逐编号核对。
        </p>
      </div>
    </header>

    <!-- 第一步：搜索导出交付物 -->
    <form class="filter-bar" @submit.prevent="onSearch">
      <label class="filter-item">
        <span>导出引用</span>
        <input
          v-model="keyword"
          data-testid="lineage-keyword"
          placeholder="导出编号 / 视图名 / 文件路径 / export-id"
        />
      </label>
      <button class="btn primary" data-testid="lineage-search" type="submit">搜索导出</button>
      <button class="btn ghost" type="button" @click="keyword = ''">清空</button>
    </form>

    <table v-if="searchResults.length" class="data-table">
      <thead>
        <tr>
          <th>导出编号</th>
          <th>视图分组（导出范围）</th>
          <th>文件路径</th>
          <th>血缘总数</th>
          <th>已验证</th>
          <th>缺口</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in searchResults" :key="item.id" data-testid="search-row">
          <td>{{ item.导出编号 }}</td>
          <td>{{ item.导出范围 }}</td>
          <td>{{ item.文件路径 }}</td>
          <td>{{ item.lineage_total }}</td>
          <td><span class="tag tag-ok">{{ item.lineage_verified }}</span></td>
          <td><span :class="['tag', item.lineage_gap ? 'tag-gap' : 'tag-muted']">{{ item.lineage_gap }}</span></td>
          <td>
            <button
              class="link"
              type="button"
              :data-testid="`query-${item.id}`"
              @click="onQuery(item.导出编号)"
            >
              查看血缘与可重放范围
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="searched && !searchResults.length" class="empty-state">未检索到匹配的导出交付物</div>

    <!-- 第二步：血缘查询结果 -->
    <template v-if="lineage">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">清单编号</span>
          <strong>{{ lineage.manifest_no }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">血缘已验证 / 总数</span>
          <strong>{{ lineage.lineage_summary.verified }} / {{ lineage.lineage_summary.total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待核实缺口</span>
          <strong :class="{ 'danger-text': lineage.lineage_summary.gap > 0 }">
            {{ lineage.lineage_summary.gap }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可执行范围（批次）</span>
          <strong>{{ lineage.executable_scope.length }}</strong>
        </article>
      </div>

      <p class="status-legend">
        视图分组：
        <span v-for="vg in lineage.view_groups" :key="vg.view_name" class="legend-item">
          {{ vg.view_name }}（{{ vg.status }}）
        </span>
      </p>

      <h3 class="section-title">当前可读来源（可进入重放）</h3>
      <table class="data-table">
        <thead>
          <tr><th>来源批次</th><th>所属视图</th><th>血缘状态</th><th>原始行数</th><th>原始记录ID</th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in lineage.readable_now" :key="entry.batch_no" data-testid="verified-row">
            <td>{{ entry.batch_no }}</td>
            <td>{{ entry.view_name }}</td>
            <td><span class="tag tag-ok">{{ entry.lineage_status }}</span></td>
            <td>{{ entry.raw_count }}</td>
            <td>{{ entry.raw_line_ids.join(', ') }}</td>
          </tr>
          <tr v-if="!lineage.readable_now.length">
            <td colspan="5" class="empty-state">暂无已核实来源，缺口补证前不可发起重放</td>
          </tr>
        </tbody>
      </table>

      <h3 class="section-title">待核实历史分组（缺口，不进可执行范围）</h3>
      <table class="data-table">
        <thead>
          <tr><th>来源批次</th><th>血缘状态</th><th>缺口原因</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="gap in lineage.pending_verification" :key="gap.batch_no" data-testid="gap-row">
            <td>{{ gap.batch_no }}</td>
            <td><span class="tag tag-gap">{{ gap.lineage_status }}</span></td>
            <td>{{ gap.reason }}</td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`verify-${gap.lineage_id}`"
                @click="onVerify(gap.lineage_id)"
              >
                补证核实
              </button>
            </td>
          </tr>
          <tr v-if="!lineage.pending_verification.length">
            <td colspan="4" class="empty-state">全部来源已核实，无缺口</td>
          </tr>
        </tbody>
      </table>

      <!-- 第三步：从检索结果发起重放 -->
      <div class="replay-bar">
        <label class="filter-item">
          <span>新解析规则版本</span>
          <input v-model="rule" data-testid="replay-rule" placeholder="dl-parse-rule-v2" />
        </label>
        <label class="filter-item">
          <span>本次最多读取批次（模拟中断）</span>
          <input v-model.number="maxBatches" data-testid="replay-max" type="number" min="1" placeholder="留空=全部" />
        </label>
        <button
          class="btn primary"
          data-testid="replay-start"
          type="button"
          :disabled="!lineage.executable_scope.length"
          @click="onStartReplay"
        >
          按可重放清单发起重放
        </button>
        <button
          class="btn"
          data-testid="replay-resume"
          type="button"
          :disabled="!lastRun || lastRun.status === '已完成'"
          @click="onResume"
        >
          从检查点续跑
        </button>
        <button class="btn ghost" data-testid="reconcile" type="button" @click="onReconcile">
          对照实际读取批次与原始行
        </button>
      </div>

      <div v-if="replayMessage" class="replay-message" data-testid="replay-message">
        <strong>{{ replayMessage }}</strong>
        <div v-if="lastRun" class="checkpoint">
          状态：{{ lastRun.status }} ｜ 检查点（上次实际读取批次）：{{ lastRun.last_read_batch || '—' }}
          ｜ 已处理：{{ lastRun.processed_batches.join(', ') || '—' }}
          ｜ 已读原文：{{ lastRun.read_raw_ids.length }} 条
        </div>
      </div>

      <!-- 第四步：核对结果 -->
      <div v-if="reconcileResult" class="reconcile-box" data-testid="reconcile-box">
        <h3 class="section-title">交付物来源清单核对</h3>
        <p>
          编号全部可核对：
          <span :class="['tag', reconcileResult.all_numbers_accounted ? 'tag-ok' : 'tag-gap']">
            {{ reconcileResult.all_numbers_accounted ? '是' : '否' }}
          </span>
        </p>
        <table class="data-table">
          <thead>
            <tr><th>来源批次</th><th>实际读取原始行</th><th>重放产出ID</th></tr>
          </thead>
          <tbody>
            <tr v-for="line in reconcileResult.read_raw_lines" :key="line.batch_no">
              <td>{{ line.batch_no }}</td>
              <td>{{ line.raw_line_ids.join(', ') }}</td>
              <td>{{ line.output_ids.join(', ') }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="reconcileResult.excluded_gaps?.length" class="danger-text">
          未进入重放的缺口：
          <span v-for="g in reconcileResult.excluded_gaps" :key="g.batch_no">
            {{ g.batch_no }}（{{ g.reason }}）
          </span>
        </p>
      </div>
    </template>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import {
  queryExportLineage,
  reconcileExport,
  resumeReplay,
  searchLineageExports,
  startReplay,
  verifyLineage,
  type LineageQueryResult,
  type LineageSearchItem,
  type ReplayRun,
} from '@/api/lineage-service'

const keyword = ref('DL-EXPORT-01')
const rule = ref('dl-parse-rule-v2')
const maxBatches = ref<number | null>(null)
const searchResults = ref<LineageSearchItem[]>([])
const lineage = ref<LineageQueryResult | null>(null)
const lastRun = ref<ReplayRun | null>(null)
const replayMessage = ref('')
const reconcileResult = ref<Record<string, any> | null>(null)
const errorMessage = ref('')
const searched = ref(false)

async function onSearch() {
  errorMessage.value = ''
  searched.value = true
  try {
    searchResults.value = await searchLineageExports(keyword.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出检索失败'
  }
}

async function onQuery(exportRef: string) {
  errorMessage.value = ''
  reconcileResult.value = null
  try {
    const result = await queryExportLineage(exportRef)
    if (!result.ok) throw new Error(result.message || '血缘查询失败')
    lineage.value = result
    lastRun.value = null
    replayMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '血缘查询失败'
  }
}

async function onVerify(lineageId: number) {
  errorMessage.value = ''
  try {
    const result = await verifyLineage(lineageId)
    if (!result.ok) throw new Error(result.message)
    if (lineage.value) await onQuery(lineage.value.export.导出编号)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补证失败'
  }
}

async function onStartReplay() {
  if (!lineage.value) return
  errorMessage.value = ''
  try {
    const result = await startReplay(
      lineage.value.manifest_no,
      rule.value,
      maxBatches.value ?? undefined,
    )
    if (!result.ok) throw new Error(result.message)
    replayMessage.value = result.message
    lastRun.value = result.run
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重放发起失败'
  }
}

async function onResume() {
  if (!lastRun.value) return
  errorMessage.value = ''
  try {
    const result = await resumeReplay(lastRun.value.id)
    if (!result.ok) throw new Error(result.message)
    replayMessage.value = result.message
    lastRun.value = result.run
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续跑失败'
  }
}

async function onReconcile() {
  if (!lineage.value) return
  errorMessage.value = ''
  try {
    reconcileResult.value = await reconcileExport(lineage.value.export.导出编号)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对失败'
  }
}

void onSearch()
</script>

<style scoped>
.lineage-page .section-title {
  margin: 18px 0 8px;
  font-size: 15px;
}
.tag {
  display: inline-block;
  min-width: 22px;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  text-align: center;
}
.tag-ok { background: #dcfce7; color: #166534; }
.tag-gap { background: #fee2e2; color: #991b1b; }
.tag-muted { background: #f1f5f9; color: #64748b; }
.replay-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
  margin: 18px 0;
  padding: 14px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.replay-message {
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 13px;
}
.checkpoint { margin-top: 6px; color: #334155; }
.reconcile-box {
  margin-top: 16px;
  padding: 14px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.danger-text { color: #b91c1c; }
.empty-state { padding: 14px; text-align: center; color: #64748b; }
</style>

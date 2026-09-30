<template>
  <section class="page" data-module="bridge">
    <header class="page-head">
      <div>
        <h2>廊桥对接管理</h2>
        <p class="page-desc">维护廊桥，围绕廊桥编号、对应机位、适用机型、对接高度做登记、筛选与状态流转。保存带版本号，冲突时以最早落库版本为准。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记廊桥</button>
        <button class="btn" type="button" @click="exportRows">导出廊桥对接清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>
    <p class="reconcile-line">
      靠接台数 {{ dockedCount }} 台（已靠接 {{ statusCount['已靠接'] }} ＋ 待撤离 {{ statusCount['待撤离'] }}），
      与下方桥位明细逐条同源；列表共 {{ total }} 座廊桥。
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ displayValue(row, column) }}</td>
          <td>v{{ row.version ?? 1 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEdit(row)">编辑</button>
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="isActionDisabled(row, action) || busy"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无廊桥对接数据，可先登记廊桥</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条廊桥对接记录</span>
      <span v-if="message" :class="['foot-msg', messageKind]">{{ message }}</span>
    </footer>

    <!-- 编辑：基于版本号保存，冲突时保留本框里用户填的内容 -->
    <div v-if="editOpen" class="modal-mask" @click.self="closeEdit">
      <div class="modal">
        <h3>编辑廊桥 {{ editRow?.['廊桥编号'] }}（基于 v{{ editForm.version }}）</h3>
        <div v-if="editConflict" class="conflict-box">
          <strong>版本冲突：</strong>{{ editConflict }}
          <div class="conflict-actions">
            <button class="btn" type="button" @click="rebaseEdit">采用最新版本重新编辑</button>
            <span>您填写的内容仍保留在表单中，也可调整后直接重试。</span>
          </div>
        </div>
        <label class="modal-field">
          <span>对接高度（厘米，适用机型：{{ editRow?.['适用机型'] }}）</span>
          <input v-model="editForm['对接高度']" placeholder="如 280 或 2.8m" />
        </label>
        <label class="modal-field">
          <span>预靠时间</span>
          <input v-model="editForm['预靠时间']" placeholder="YYYY-MM-DD HH:mm" />
        </label>
        <label class="modal-field">
          <span>操作人员</span>
          <input v-model="editForm['操作人员']" />
        </label>
        <div class="modal-actions">
          <button class="btn primary" type="button" :disabled="saving" @click="saveEdit">
            {{ saving ? '保存中…' : '保存' }}
          </button>
          <button class="btn ghost" type="button" :disabled="saving" @click="closeEdit">取消</button>
        </div>
      </div>
    </div>

    <!-- 靠接登记 -->
    <div v-if="dockOpen" class="modal-mask" @click.self="dockOpen = false">
      <div class="modal">
        <h3>靠接廊桥 {{ dockRow?.['廊桥编号'] }}</h3>
        <label class="modal-field">
          <span>预靠时间 *</span>
          <input v-model="dockForm['预靠时间']" placeholder="YYYY-MM-DD HH:mm" />
        </label>
        <label class="modal-field">
          <span>操作人员</span>
          <input v-model="dockForm['操作人员']" />
        </label>
        <p class="modal-hint">同一预靠时间的重复靠接登记会被按时间去重；连点提交只认第一次落库。</p>
        <div class="modal-actions">
          <button class="btn primary" type="button" :disabled="saving" @click="confirmDock">确认靠接</button>
          <button class="btn ghost" type="button" :disabled="saving" @click="dockOpen = false">取消</button>
        </div>
      </div>
    </div>

    <!-- 详情：直接读 GET /{id}，与列表同一份数据 -->
    <div v-if="detailOpen" class="modal-mask" @click.self="detailOpen = false">
      <div class="modal modal-wide">
        <h3>廊桥详情 {{ detailRow?.['廊桥编号'] }}</h3>
        <table class="data-table detail-table">
          <tbody>
            <tr v-for="item in detailPairs" :key="item.label">
              <th>{{ item.label }}</th>
              <td>{{ item.value || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <h4>靠接/撤离记录</h4>
        <table class="data-table">
          <thead>
            <tr><th>序号</th><th>动作</th><th>预靠时间</th><th>撤桥时间</th><th>对接高度</th><th>操作人员</th><th>登记时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="rec in (detailRow?.['靠接记录'] as DockRecord[] | undefined) ?? []" :key="rec.序号">
              <td>{{ rec.序号 }}</td>
              <td>{{ rec.动作 }}</td>
              <td>{{ rec.预靠时间 }}</td>
              <td>{{ rec.撤桥时间 }}</td>
              <td>{{ rec.对接高度 }}</td>
              <td>{{ rec.操作人员 }}</td>
              <td>{{ rec.登记时间 }}</td>
            </tr>
            <tr v-if="!((detailRow?.['靠接记录'] as DockRecord[] | undefined)?.length)">
              <td colspan="7" class="empty-state">暂无靠接记录</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn" type="button" @click="detailOpen = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- 登记廊桥 -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <div class="modal">
        <h3>登记廊桥</h3>
        <label v-for="field in createFields" :key="field" class="modal-field">
          <span>{{ field }}{{ field === '廊桥编号' || field === '对应机位' || field === '适用机型' ? ' *' : '' }}</span>
          <input v-model="createForm[field]" />
        </label>
        <div class="modal-actions">
          <button class="btn primary" type="button" :disabled="saving" @click="confirmCreate">提交登记</button>
          <button class="btn ghost" type="button" :disabled="saving" @click="createOpen = false">取消</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type DockRecord = {
  序号: number
  动作: string
  预靠时间: string
  撤桥时间: string
  对接高度: string
  操作人员: string
  登记时间: string
}
type ActionJson = { ok: boolean; message: string; code?: string; entry?: Row | null }
type StatsJson = {
  total: number
  docked: number
  by_status: Record<string, number>
}

const ENDPOINT = '/api/bridge'
const columns = ["廊桥编号", "对应机位", "适用机型", "对接高度", "预靠时间", "撤桥时间", "操作人员", "廊桥状态"]
const actions = ["靠接廊桥", "撤离廊桥", "登记检修"]
const statuses = ["待靠接", "已靠接", "待撤离", "检修中"]
const createFields = ["廊桥编号", "对应机位", "适用机型", "对接高度", "预靠时间", "操作人员"]
const EDITABLE = ["对接高度", "预靠时间", "操作人员"]

const rows = ref<Row[]>([])
const total = ref(0)
const busy = ref(false)
const saving = ref(false)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const message = ref('')
const messageKind = ref<'error-text' | 'ok-text'>('error-text')
function notice(text: string, kind: 'error-text' | 'ok-text' = 'error-text') {
  message.value = text
  messageKind.value = kind
}

const statusCount = reactive<Record<string, number>>({
  待靠接: 0,
  已靠接: 0,
  待撤离: 0,
  检修中: 0,
})
const dockedCount = ref(0)
const stats = computed(() => [
  { label: '待靠接廊桥', value: statusCount['待靠接'] },
  { label: '靠接台数（已靠接+待撤离）', value: dockedCount.value },
  { label: '其中已靠接', value: statusCount['已靠接'] },
  { label: '检修中廊桥', value: statusCount['检修中'] },
])

function displayValue(row: Row, column: string): string | number | null {
  const value = row[column]
  if (column === '撤桥时间' && !value && row['status'] === '待靠接') return '—'
  return value ?? '—'
}

function isActionDisabled(row: Row, action: string): boolean {
  const status = String(row['status'] ?? '')
  if (status === '检修中') return true
  if (action === '靠接廊桥') return status === '已靠接' || status === '待撤离'
  if (action === '撤离廊桥') return status === '待靠接'
  return false
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

// ------------------------------------------------------------ 列表与总览
async function reloadStats() {
  const response = await request(`${ENDPOINT}/stats`)
  if (!response.ok) return
  const data = (await response.json()) as StatsJson
  dockedCount.value = data.docked
  for (const status of statuses) {
    statusCount[status] = data.by_status[status] ?? 0
  }
}

async function reload() {
  notice('', 'error-text')
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('廊桥列表读取失败')
    }
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
    await reloadStats()
  } catch (error) {
    notice(error instanceof Error ? error.message : '廊桥对接列表读取失败')
  }
}

// ------------------------------------------------------------ 编辑（版本号）
const editOpen = ref(false)
const editRow = ref<Row | null>(null)
const editConflict = ref('')
// 每次打开编辑生成一个幂等令牌：整轮保存/重试复用同一个，重复提交只认第一次。
let editToken = ''
const editForm = reactive<Row>({})

function openEdit(row: Row) {
  editRow.value = row
  editConflict.value = ''
  editToken = newToken()
  editForm.version = row.version ?? 1
  for (const field of EDITABLE) {
    editForm[field] = (row[field] as string) ?? ''
  }
  editOpen.value = true
}

function closeEdit() {
  editOpen.value = false
  editRow.value = null
  editConflict.value = ''
}

async function saveEdit() {
  if (!editRow.value) return
  saving.value = true
  try {
    const values: Row = {
      version: editForm.version,
      request_token: editToken,
    }
    for (const field of EDITABLE) {
      values[field] = editForm[field]
    }
    const response = await request(`${ENDPOINT}/${editRow.value.id}`, {
      method: 'PUT',
      body: JSON.stringify({ values }),
    })
    const result = (await response.json()) as ActionJson
    if (result.ok) {
      notice(result.message || '廊桥对接记录已保存', 'ok-text')
      editOpen.value = false
      await reload()
      return
    }
    // 冲突：保留用户在表单里填的内容，提示已经有人先落库，版本基线刷新到当前版。
    if (result.code === 'conflict') {
      editConflict.value = result.message
      editForm.version = result.entry?.version ?? editForm.version
      return
    }
    // 高度超范围/检修锁定等：同样不关弹窗、不清表单。
    notice(result.message || '保存未生效')
  } catch (error) {
    notice(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function rebaseEdit() {
  if (!editRow.value) return
  try {
    const response = await request(`${ENDPOINT}/${editRow.value.id}`)
    if (!response.ok) throw new Error('读取最新廊桥详情失败')
    const latest = (await response.json()) as Row
    openEdit(latest)
  } catch (error) {
    notice(error instanceof Error ? error.message : '读取最新廊桥详情失败')
  }
}

// ------------------------------------------------------------ 靠接登记
const dockOpen = ref(false)
const dockRow = ref<Row | null>(null)
const dockForm = reactive<Row>({})
let dockToken = ''

function runAction(action: string, row: Row) {
  if (action === '靠接廊桥') {
    dockRow.value = row
    dockToken = newToken()
    dockForm['预靠时间'] = (row['预靠时间'] as string) ?? ''
    dockForm['操作人员'] = (row['操作人员'] as string) ?? ''
    dockOpen.value = true
    return
  }
  if (action === '撤离廊桥') {
    if (!window.confirm(`确认撤离廊桥 ${row['廊桥编号']}？撤桥时间将按当前时刻记录。`)) return
  }
  if (action === '登记检修') {
    if (!window.confirm(`确认将廊桥 ${row['廊桥编号']} 登记为检修？登记后将冻结靠接变更。`)) return
  }
  void submitAction(action, row, { request_token: newToken() })
}

async function confirmDock() {
  if (!dockRow.value) return
  if (!String(dockForm['预靠时间'] ?? '').trim()) {
    notice('请填写预靠时间')
    return
  }
  const row = dockRow.value
  dockOpen.value = false
  await submitAction('靠接廊桥', row, {
    预靠时间: dockForm['预靠时间'],
    操作人员: dockForm['操作人员'],
    request_token: dockToken,
  })
}

async function submitAction(action: string, row: Row, extra: Record<string, unknown>) {
  busy.value = true
  saving.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, ...extra } }),
    })
    const result = (await response.json()) as ActionJson
    notice(
      result.message || (result.ok ? '操作已生效' : '操作未生效'),
      result.ok ? 'ok-text' : 'error-text',
    )
    await reload()
  } catch (error) {
    notice(error instanceof Error ? error.message : '廊桥对接操作失败')
  } finally {
    busy.value = false
    saving.value = false
  }
}

// ------------------------------------------------------------ 详情
const detailOpen = ref(false)
const detailRow = ref<Row | null>(null)
const detailPairs = computed(() => {
  const row = detailRow.value ?? {}
  return [
    { label: '廊桥编号', value: row['廊桥编号'] },
    { label: '对应机位', value: row['对应机位'] },
    { label: '适用机型', value: row['适用机型'] },
    { label: '对接高度', value: row['对接高度'] },
    { label: '预靠时间', value: row['预靠时间'] },
    { label: '撤桥时间', value: row['撤桥时间'] },
    { label: '操作人员', value: row['操作人员'] },
    { label: '廊桥状态', value: row['廊桥状态'] },
    { label: '版本号', value: `v${row.version ?? 1}` },
    { label: '最近更新', value: row['更新时间'] },
  ]
})

async function openDetail(row: Row) {
  detailRow.value = row
  detailOpen.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('廊桥详情读取失败')
    detailRow.value = (await response.json()) as Row
  } catch (error) {
    notice(error instanceof Error ? error.message : '廊桥详情读取失败')
  }
}

// ------------------------------------------------------------ 登记
const createOpen = ref(false)
const createForm = reactive<Record<string, string>>({})

function openCreate() {
  for (const field of createFields) createForm[field] = ''
  createOpen.value = true
}

async function confirmCreate() {
  saving.value = true
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...createForm } }),
    })
    const result = (await response.json()) as ActionJson
    if (!result.ok) {
      notice(result.message || '登记失败')
      return
    }
    notice(result.message, 'ok-text')
    createOpen.value = false
    await reload()
  } catch (error) {
    notice(error instanceof Error ? error.message : '廊桥登记失败')
  } finally {
    saving.value = false
  }
}

function newToken(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `t-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.reconcile-line { margin: 0 0 12px; font-size: 12px; color: var(--muted); }
.ok-text { color: #027a48; }
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal {
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
  width: 420px;
  max-height: 86vh;
  overflow: auto;
}
.modal-wide { width: 760px; }
.modal h3 { margin: 0 0 12px; font-size: 16px; }
.modal h4 { margin: 14px 0 8px; font-size: 14px; }
.modal-field { display: block; margin-bottom: 10px; }
.modal-field span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.modal-field input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.modal-hint { font-size: 12px; color: var(--muted); margin: 4px 0; }
.conflict-box {
  border: 1px solid #fda29b;
  background: #fef3f2;
  color: #b42318;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  margin-bottom: 12px;
}
.conflict-actions { display: flex; align-items: center; gap: 10px; margin-top: 8px; flex-wrap: wrap; }
.conflict-actions span { font-size: 12px; color: var(--muted); }
.detail-table th { width: 110px; }
.link:disabled { color: #94a3b8; cursor: not-allowed; }
</style>

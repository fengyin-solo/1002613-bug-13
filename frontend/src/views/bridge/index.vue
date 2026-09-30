<template>
  <section class="page" data-module="bridge">
    <header class="page-head">
      <div>
        <h2>廊桥对接管理</h2>
        <p class="page-desc">维护廊桥，围绕廊桥编号、对应机位、适用机型、对接高度做登记、筛选与状态流转。保存与动作均带版本号，并发冲突以最早落库为准。</p>
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

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>廊桥状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
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
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>v{{ row.version ?? 1 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEdit(row)">编辑保存</button>
            <template v-if="row.status !== '检修中'">
              <button
                class="link"
                type="button"
                :disabled="busyKey === actionKey('靠接廊桥', row) || row.status === '已靠接'"
                :title="row.status === '已靠接' ? '当前已靠接，不能重复登记' : ''"
                @click="runAction('靠接廊桥', row)"
              >
                靠接廊桥
              </button>
              <button
                class="link"
                type="button"
                :disabled="busyKey === actionKey('撤离廊桥', row) || (row.status !== '已靠接' && row.status !== '待撤离')"
                @click="runAction('撤离廊桥', row)"
              >
                撤离廊桥
              </button>
              <button
                class="link"
                type="button"
                :disabled="busyKey === actionKey('登记检修', row)"
                @click="runAction('登记检修', row)"
              >
                登记检修
              </button>
            </template>
            <span v-else class="locked-hint">检修中，冻结靠接变更</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无廊桥对接数据，可先登记廊桥</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条廊桥对接记录</span>
      <span class="foot-msg">
        <span v-if="conflictMessage" class="conflict-text">{{ conflictMessage }}</span>
        <span v-else-if="errorMessage" class="error-text">{{ errorMessage }}</span>
        <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
      </span>
    </footer>

    <!-- 编辑保存弹层：冲突时保留用户已填内容，不直接覆盖 -->
    <div v-if="editing" class="modal-mask" @click.self="closeEdit">
      <div class="modal">
        <h3>编辑廊桥 {{ editing.row['廊桥编号'] }}（基于 v{{ editing.baseVersion }}）</h3>
        <div v-if="editing.conflict" class="conflict-box">
          <strong>版本冲突：</strong>{{ editing.conflict }}
          <div v-if="editing.serverEntry" class="conflict-detail">
            对方已落库内容：对接高度 {{ editing.serverEntry['对接高度'] ?? '—' }}，
            预靠时间 {{ editing.serverEntry['预靠时间'] ?? '—' }}，
            操作人员 {{ editing.serverEntry['操作人员'] ?? '—' }}
          </div>
          <div class="conflict-actions">
            <button class="btn" type="button" @click="reloadLatestIntoDialog">读取对方最新版本（您填写的内容会被替换）</button>
            <button class="btn ghost" type="button" @click="editing.conflict = ''">继续保留我的内容</button>
          </div>
        </div>
        <div v-else-if="editing.error" class="error-box">{{ editing.error }}</div>
        <label class="form-item">
          <span>对接高度（米）</span>
          <input v-model="editing.form['对接高度']" placeholder="如 3.6，须在适用机型高度区间内" />
        </label>
        <label class="form-item">
          <span>预靠时间</span>
          <input v-model="editing.form['预靠时间']" placeholder="YYYY-MM-DD HH:mm" />
        </label>
        <label class="form-item">
          <span>操作人员</span>
          <input v-model="editing.form['操作人员']" placeholder="操作人员姓名" />
        </label>
        <p class="form-hint">适用机型：{{ editing.row['适用机型'] }}；保存时后端会先比对版本，再校验高度是否超出机型范围。</p>
        <div class="modal-actions">
          <button class="btn primary" type="button" :disabled="editing.saving" @click="saveEdit">
            {{ editing.saving ? '保存中…' : '保存' }}
          </button>
          <button class="btn ghost" type="button" :disabled="editing.saving" @click="closeEdit">取消</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type SaveOutcome = {
  ok: boolean
  message: string
  entry: Row | null
}

const ENDPOINT = '/api/bridge'
const columns = ['廊桥编号', '对应机位', '适用机型', '对接高度', '预靠时间', '撤桥时间', '操作人员', '廊桥状态']
const statuses = ['待靠接', '已靠接', '待撤离', '检修中']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const conflictMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = ['廊桥编号', '对应机位', '适用机型']
const busyKey = ref('')

const summary = ref<Record<string, number>>({})

// 统计卡与桥位明细同源：数字都来自 /api/bridge/summary 对同一份 store 记录的逐条计数
const stats = computed(() => [
  { label: '待靠接', value: summary.value['待靠接'] ?? 0 },
  { label: '靠接台数（已靠接+待撤离）', value: summary.value['靠接台数'] ?? 0 },
  { label: '已靠接', value: summary.value['已靠接'] ?? 0 },
  { label: '待撤离', value: summary.value['待撤离'] ?? 0 },
  { label: '检修中廊桥', value: summary.value['检修中'] ?? 0 },
])

type EditingState = {
  row: Row
  baseVersion: number
  form: Record<string, string>
  conflict: string
  error: string
  serverEntry: Row | null
  saving: boolean
}

const editing = ref<EditingState | null>(null)

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '廊桥登记入口尚未接入审批流'
}

function flash(kind: 'error' | 'success' | 'conflict', message: string) {
  errorMessage.value = kind === 'error' ? message : ''
  successMessage.value = kind === 'success' ? message : ''
  conflictMessage.value = kind === 'conflict' ? message : ''
}

function actionKey(action: string, row: Row) {
  return `${row.id}:${action}:v${row.version ?? 1}`
}

async function postAction(action: string, row: Row) {
  // 幂等令牌只与「桥+动作+所依据版本」绑定：同一意图的重复提交只认第一次落库
  const requestId = `act-${row.id}-${action}-v${row.version ?? 1}`
  const response = await request(`${ENDPOINT}/${row.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({
      values: { action, version: row.version ?? 1, request_id: requestId },
    }),
  })
  return (await response.json()) as SaveOutcome
}

async function runAction(action: string, row: Row) {
  flash('success', '')
  busyKey.value = actionKey(action, row)
  try {
    const outcome = await postAction(action, row)
    if (outcome.ok) {
      flash('success', outcome.message)
      await reload()
    } else {
      // 冲突/重复登记/检修拦截等业务拒绝：消息写明原因，列表不做乐观覆盖
      flash('error', outcome.message)
    }
  } catch (error) {
    flash('error', error instanceof Error ? error.message : '廊桥对接操作失败')
  } finally {
    busyKey.value = ''
  }
}

function openEdit(row: Row) {
  flash('success', '')
  editing.value = {
    row,
    baseVersion: Number(row.version ?? 1),
    form: {
      对接高度: String(row['对接高度'] ?? ''),
      预靠时间: String(row['预靠时间'] ?? ''),
      操作人员: String(row['操作人员'] ?? ''),
    },
    conflict: '',
    error: '',
    serverEntry: null,
    saving: false,
  }
}

function closeEdit() {
  editing.value = null
}

async function reloadLatestIntoDialog() {
  if (!editing.value) return
  const response = await request(`${ENDPOINT}/${editing.value.row.id}`)
  if (!response.ok) {
    editing.value.error = '最新版本读取失败，请稍后重试'
    return
  }
  const latest = (await response.json()) as Row
  editing.value.row = latest
  editing.value.baseVersion = Number(latest.version ?? 1)
  editing.value.form = {
    对接高度: String(latest['对接高度'] ?? ''),
    预靠时间: String(latest['预靠时间'] ?? ''),
    操作人员: String(latest['操作人员'] ?? ''),
  }
  editing.value.conflict = ''
  editing.value.serverEntry = null
}

async function saveEdit() {
  if (!editing.value) return
  const state = editing.value
  state.error = ''
  state.saving = true
  try {
    // 同一次编辑会话使用同一令牌：网络重试/重复点击不会产生第二份落库
    const requestId = `save-${state.row.id}-v${state.baseVersion}`
    const response = await request(`${ENDPOINT}/${state.row.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        values: {
          ...state.form,
          version: state.baseVersion,
          request_id: requestId,
        },
      }),
    })
    const outcome = (await response.json()) as SaveOutcome
    if (outcome.ok) {
      editing.value = null
      flash('success', outcome.message)
      await reload()
      return
    }
    if (outcome.message.includes('版本冲突') || outcome.message.includes('已被他人更新')) {
      // 关键规则：冲突时表单里用户填的内容原样保留，只提示、不覆盖
      state.conflict = outcome.message
      state.serverEntry = outcome.entry
      flash('conflict', outcome.message)
    } else {
      // 对接高度超范围、检修冻结等校验失败：保留填写内容并写明原因
      state.error = outcome.message
      flash('error', outcome.message)
    }
  } catch (error) {
    state.error = error instanceof Error ? error.message : '保存请求未送达'
  } finally {
    state.saving = false
  }
}

async function reload() {
  const params = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) params.set(key, value)
  })
  if (statusFilter.value) params.set('status', statusFilter.value)
  try {
    const [listResp, summaryResp] = await Promise.all([
      request(`${ENDPOINT}?${params.toString()}`),
      request(`${ENDPOINT}/summary`),
    ])
    if (!listResp.ok) throw new Error('廊桥列表读取失败')
    if (!summaryResp.ok) throw new Error('廊桥汇总读取失败')
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    summary.value = await summaryResp.json()
  } catch (error) {
    flash('error', error instanceof Error ? error.message : '廊桥对接列表读取失败')
  }
}

onMounted(reload)
</script>

<style scoped>
.foot-msg {
  display: flex;
  gap: 8px;
}
.success-text {
  color: #027a48;
}
.conflict-text {
  color: #b54708;
}
.locked-hint {
  color: #b54708;
  font-size: 12px;
}
.link:disabled {
  color: #94a3b8;
  cursor: not-allowed;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal {
  width: 460px;
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.2);
}
.modal h3 {
  margin: 0 0 12px;
  font-size: 15px;
}
.form-item {
  display: block;
  margin-bottom: 10px;
}
.form-item span {
  display: block;
  font-size: 12px;
  color: #64748b;
  margin-bottom: 4px;
}
.form-item input {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid #d8dee6;
  border-radius: 6px;
}
.form-hint {
  font-size: 12px;
  color: #64748b;
  margin: 4px 0 12px;
}
.modal-actions,
.conflict-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.conflict-box {
  border: 1px solid #f79009;
  background: #fffaeb;
  color: #b54708;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
  margin-bottom: 12px;
}
.conflict-detail {
  margin-top: 6px;
}
.conflict-actions {
  margin-top: 8px;
}
.error-box {
  border: 1px solid #fda29b;
  background: #fef3f2;
  color: #b42318;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
  margin-bottom: 12px;
}
</style>

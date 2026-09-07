<template>
  <div>
    <div class="stat-cards">
      <div class="mc-card stat-card">
        <div class="stat-label">服务器状态</div>
        <div class="stat-value" :class="s.online ? 'online' : 'offline'">{{ s.online ? '在线' : '离线' }}</div>
      </div>
      <div class="mc-card stat-card">
        <div class="stat-label">TPS</div>
        <div class="stat-value">{{ s.tps.toFixed(2) }}</div>
      </div>
      <div class="mc-card stat-card">
        <div class="stat-label">在线玩家</div>
        <div class="stat-value">{{ s.players }}</div>
      </div>
      <div class="mc-card stat-card">
        <div class="stat-label">最近心跳</div>
        <div class="stat-value" style="font-size:17px;line-height:34px">{{ s.lastBeatText }}</div>
      </div>
    </div>

    <div class="mc-card" style="margin-top:16px;padding:14px 18px">
      <div style="font-size:13px;color:var(--mc-sub);margin-bottom:10px">快捷命令</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <el-button v-for="c in quicks" :key="c.cmd" size="small" @click="quick(c)">{{ c.cmd }}</el-button>
      </div>
    </div>

    <div class="mc-card" style="margin-top:16px;padding:14px 18px">
      <div style="display:flex;gap:10px">
        <el-input v-model="cmd" placeholder="输入 MC 命令（无需开头的 /），如 give @a command_block" clearable
                  @keyup.enter="send" />
        <el-button type="primary" :loading="sending" data-test="cmd-send" @click="send">发送</el-button>
      </div>
      <div class="mc-logbox" ref="logBox" v-html="logHtml"></div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { api, MC_PORT } from '../api'

const s = reactive({ online: false, tps: 0, players: 0, last_beat: null })
const cmd = ref('')
const sending = ref(false)
const logs = ref([])
const logBox = ref(null)
let es = null
let timer = null

const quicks = [
  { cmd: '/list', run: true }, { cmd: '/tps', run: true }, { cmd: '/say ', run: false },
  { cmd: '/give @a command_block', run: true }, { cmd: '/op ', run: false },
  { cmd: '/deop ', run: false }, { cmd: '/gamemode ', run: false },
]

const s_time = (t) => t ? new Date((t || 0) * 1000).toLocaleTimeString() : '-'
const lastBeatText = computed(() => s_time(s.last_beat))
const logHtml = computed(() => logs.value.map(l => {
  let cls = ''
  if (l[0] === '[sys]') cls = 'c-sys'
  else if (l[0] === '[conn]') cls = 'c-sys'
  else if (l[0] && l[0].startsWith('[admin')) cls = 'c-admin'
  else if (l[0] && (l[0].startsWith('[mc') || l[0].startsWith('[MC]'))) cls = 'c-mc'
  const text = esc(l[1])
  return `<div><span class="t">${esc(l[0])}</span><span class="${cls}">${text}</span></div>`
}).join(''))

function esc(t) {
  const d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML
}

function append(line) {
  const t = new Date().toLocaleTimeString()
  logs.value.push([`[${t}]`, line])
  if (logs.value.length > 500) logs.value.splice(0, logs.value.length - 500)
  nextTick(() => { if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight })
}

function quick(q) {
  if (q.run) send(q.cmd)
  else { cmd.value = q.cmd; }
}

async function send(text) {
  const c = (text == null ? cmd.value : text).trim()
  if (!c) return
  sending.value = true
  try {
    const d = await api.cmd(c.replace(/^\//, ''))
    if (d.ok) {
      append(`[admin] /${c}`)
      const wf = d.workflow_count > 1 ? `（${d.workflow_count} 个工作流）` : (d.workflow || '')
      if (wf) append(`[wf] ✅ 命中工作流 ${wf}`)
      if (d.result) String(d.result).split('\n').forEach(l => l && append(l))
    } else {
      append(`[error] ${d.error || '发送失败'}`)
    }
  } catch (e) { append('[error] 请求失败') }
  finally { sending.value = false; cmd.value = '' }
}

async function loadStatus() {
  try {
    const d = await api.status()
    if (d.ok && d.data) {
      s.online = !!d.data.online; s.tps = Number(d.data.tps) || 0
      s.players = Number(d.data.players) || 0; s.last_beat = d.data.last_beat || null
    }
  } catch (e) {}
}

function connectSSE() {
  try {
    es = new EventSource(`http://${location.hostname}:${MC_PORT}/api/stream`)
    es.onmessage = (e) => append(e.data)
    es.onerror = () => { try { es.close() } catch (_) {}; setTimeout(connectSSE, 3000) }
  } catch (e) {}
}

onMounted(() => { loadStatus(); timer = setInterval(loadStatus, 3000); connectSSE() })
onUnmounted(() => { if (timer) clearInterval(timer); if (es) es.close() })
</script>

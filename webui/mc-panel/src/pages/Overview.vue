<template>
  <div>
    <!-- 1Panel 风格统计卡 -->
    <div class="stat-cards">
      <div class="mc-card stat-card">
        <div class="stat-icon" :class="s.online ? 'i-green' : 'i-red'">⚡</div>
        <div class="stat-info">
          <div class="v" :style="{ color: s.online ? '#35c07c' : '#f56c6c' }">{{ s.online ? '运行中' : '已离线' }}</div>
          <div class="l">服务器状态</div>
        </div>
      </div>
      <div class="mc-card stat-card">
        <div class="stat-icon i-blue">🎯</div>
        <div class="stat-info">
          <div class="v">{{ s.tps.toFixed(2) }}</div>
          <div class="l">当前 TPS（均值 20）</div>
        </div>
      </div>
      <div class="mc-card stat-card">
        <div class="stat-icon i-purple">🧑‍🤝‍🧑</div>
        <div class="stat-info">
          <div class="v">{{ s.players }}</div>
          <div class="l">在线玩家</div>
        </div>
      </div>
      <div class="mc-card stat-card">
        <div class="stat-icon i-gray">🕐</div>
        <div class="stat-info">
          <div class="v" style="font-size:16px;padding-top:6px">{{ lastBeat }}</div>
          <div class="l">最近心跳</div>
        </div>
      </div>
    </div>

    <!-- 1Panel 风格：白卡图表，左右两栏 -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;margin-top:16px">
      <div class="mc-card chart-card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div>
            <div class="mc-title">TPS 趋势</div>
            <div class="mc-sub">过去约 16 分钟 · 平均 {{ avgTps }}</div>
          </div>
          <el-tag size="small" type="success" effect="plain">实时</el-tag>
        </div>
        <div ref="tpsEl" style="height:230px"></div>
      </div>
      <div class="mc-card chart-card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div>
            <div class="mc-title">在线玩家</div>
            <div class="mc-sub">峰值 {{ peakPlayers }} 人</div>
          </div>
          <el-tag size="small" type="primary" effect="plain">实时</el-tag>
        </div>
        <div ref="playersEl" style="height:230px"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'

const s = ref({ online: false, tps: 0, players: 0, last_beat: null })
const avgTps = ref('0.0')
const peakPlayers = ref(0)
const tpsEl = ref(null)
const playersEl = ref(null)
let charts = []
let timer = null

const lastBeat = computed(() => s.value.last_beat ? new Date(s.value.last_beat * 1000).toLocaleTimeString() : '-')

const AXIS = { axisLine: { lineStyle: { color: '#e4e7ed' } }, axisLabel: { color: '#8b93a7', fontSize: 10 } }
const GRID = { left: 46, right: 16, top: 12, bottom: 24 }
const DARK = document.documentElement.classList.contains('dark')

function mkOption(name, color, data) {
  const gridLine = DARK ? '#232834' : '#eef0f3'
  return {
    grid: GRID, animation: false,
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: data.map(d => d.label), boundaryGap: false, ...AXIS },
    yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: gridLine } }, ...AXIS },
    series: [{
      name, type: 'line', data: data.map(d => d.value), showSymbol: false, smooth: true,
      lineStyle: { color, width: 2 }, itemStyle: { color },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: hexA(color, .16) }, { offset: 1, color: hexA(color, 0) }] } },
    }],
  }
}
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

async function loadStatus() {
  try {
    const d = await api.status()
    if (d.ok && d.data) {
      s.value = { online: !!d.data.online, tps: Number(d.data.tps) || 0, players: Number(d.data.players) || 0, last_beat: d.data.last_beat || null }
    }
  } catch (e) {}
}

async function loadCharts() {
  try {
    const d = await api.heartbeat()
    const raw = d.ok && Array.isArray(d.data) ? d.data : []
    if (!raw.length) return
    const step = Math.max(1, Math.ceil(raw.length / 220))
    const pts = []
    for (let i = 0; i < raw.length; i += step) pts.push(raw[i])
    const mk = (k) => pts.map(p => ({ value: Number(p[k]) || 0, label: new Date((p.time || 0) * 1000).toLocaleTimeString() }))
    const tps = mk('tps'); const players = mk('players')
    avgTps.value = (tps.reduce((a, b) => a + b.value, 0) / Math.max(1, tps.length)).toFixed(1)
    peakPlayers.value = players.reduce((a, b) => Math.max(a, b.value), 0)
    if (!charts[0] && tpsEl.value) charts[0] = echarts.init(tpsEl.value)
    if (!charts[1] && playersEl.value) charts[1] = echarts.init(playersEl.value)
    charts[0] && charts[0].setOption(mkOption('TPS', '#005eeb', tps), true)
    charts[1] && charts[1].setOption(mkOption('玩家', '#35c07c', players), true)
  } catch (e) {}
}

function resize() { charts.forEach(c => c && c.resize()) }

onMounted(() => {
  loadStatus(); loadCharts()
  timer = setInterval(() => { loadStatus(); loadCharts() }, 5000)
  window.addEventListener('resize', resize)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', resize)
  charts.forEach(c => c && c.dispose()); charts = []
})
</script>

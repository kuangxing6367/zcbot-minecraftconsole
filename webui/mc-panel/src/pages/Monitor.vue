<template>
  <div>
    <div class="mc-card" style="padding:14px 18px;display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <span style="font-size:13.5px">运行历史（每秒心跳 × N 条，自动降采样展示）</span>
      <el-button size="small" :loading="loading" data-test="monitor-refresh" @click="load">刷新</el-button>
    </div>
    <div class="mc-card" style="padding:16px 18px;margin-bottom:16px">
      <div style="font-size:13px;color:var(--mc-sub);margin-bottom:10px">TPS（平均 <b style="color:#35c07c">{{ avgTps }}</b>）</div>
      <div ref="tpsEl" style="height:220px"></div>
    </div>
    <div class="mc-card" style="padding:16px 18px">
      <div style="font-size:13px;color:var(--mc-sub);margin-bottom:10px">在线玩家（峰值 <b style="color:#6ea1ff">{{ peakPlayers }}</b>）</div>
      <div ref="playersEl" style="height:220px"></div>
    </div>
    <el-empty v-if="!loading && empty" description="暂无心跳数据（服务器在线后每秒自动记录）" />
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'

const loading = ref(false)
const empty = ref(false)
const avgTps = ref('0.0')
const peakPlayers = ref(0)
const tpsEl = ref(null)
const playersEl = ref(null)
let charts = []
let timer = null

const AXIS = { axisLine: { lineStyle: { color: '#333b4a' } }, axisLabel: { color: '#8b93a7', fontSize: 10 } }
const GRID = { left: 48, right: 18, top: 14, bottom: 26 }

function mkOption(name, color, data) {
  return {
    grid: GRID, animation: false,
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: data.map(d => d.label), boundaryGap: false, ...AXIS },
    yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#232834' } }, ...AXIS },
    series: [{
      name, type: 'line', data: data.map(d => d.value), showSymbol: false, smooth: true,
      lineStyle: { color, width: 1.8 }, itemStyle: { color },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: hexA(color, .22) }, { offset: 1, color: hexA(color, 0) }] } },
    }],
  }
}
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

async function load() {
  loading.value = true
  try {
    const d = await api.heartbeat()
    const raw = d.ok && Array.isArray(d.data) ? d.data : []
    empty.value = raw.length === 0
    if (!raw.length) return
    const step = Math.max(1, Math.ceil(raw.length / 240))
    const pts = []
    for (let i = 0; i < raw.length; i += step) pts.push(raw[i])
    const tps = pts.map(p => ({ value: Number(p.tps) || 0, label: new Date((p.time || 0) * 1000).toLocaleTimeString() }))
    const players = pts.map(p => ({ value: Number(p.players) || 0, label: new Date((p.time || 0) * 1000).toLocaleTimeString() }))
    avgTps.value = (tps.reduce((a, b) => a + b.value, 0) / Math.max(1, tps.length)).toFixed(1)
    peakPlayers.value = players.reduce((a, b) => Math.max(a, b.value), 0)

    if (!charts[0] && tpsEl.value) charts[0] = echarts.init(tpsEl.value)
    if (!charts[1] && playersEl.value) charts[1] = echarts.init(playersEl.value)
    charts[0] && charts[0].setOption(mkOption('TPS', '#35c07c', tps), true)
    charts[1] && charts[1].setOption(mkOption('玩家', '#6ea1ff', players), true)
  } finally { loading.value = false }
}

function resize() { charts.forEach(c => c && c.resize()) }

onMounted(() => { load(); timer = setInterval(load, 8000); window.addEventListener('resize', resize) })
onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', resize)
  charts.forEach(c => c && c.dispose())
  charts = []
})
</script>

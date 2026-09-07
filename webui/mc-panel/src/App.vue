<template>
  <!-- 未登录 → 登录页 -->
  <div v-if="!ready" class="login-wrap">
    <el-card class="login-card" shadow="always">
      <div class="login-title">
        <div class="logo">⛏</div>
        <h1>minecraftconsole</h1>
        <p>登录后进入 MC 控制台</p>
      </div>
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" size="large" data-test="login-user" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" size="large" show-password
                    data-test="login-pass" @keyup.enter="doLogin" />
        </el-form-item>
        <el-button type="primary" size="large" style="width:100%" :loading="busy" data-test="login-btn"
                   @click="doLogin">登 录</el-button>
        <p v-if="err" style="color:var(--el-color-danger);font-size:12px;margin:10px 0 0;text-align:center">{{ err }}</p>
      </el-form>
    </el-card>
  </div>

  <!-- 已登录 → 主框架 -->
  <div v-else class="app" style="display:flex;height:100vh">
    <aside class="panel-sidebar">
      <div class="panel-brand"><span class="logo">⛏</span><b>minecraftconsole</b></div>
      <nav class="panel-menu">
        <div v-for="(g, gi) in menus" :key="gi">
          <div class="menu-group">{{ g.label }}</div>
          <div v-for="m in g.items" :key="m.key" class="menu-item" :class="{ active: page === m.key }"
               @click="switchPage(m.key)">
            <span class="icon">{{ m.icon }}</span><span>{{ m.label }}</span>
          </div>
        </div>
      </nav>
      <div class="panel-foot">
        <div class="uinfo">
          <el-avatar :size="26" style="background:#3370ff">{{ (me.username || 'U')[0].toUpperCase() }}</el-avatar>
          <span>{{ me.username }}</span>
        </div>
        <el-tooltip content="退出登录"><el-button text circle @click="doLogout">⎋</el-button></el-tooltip>
      </div>
    </aside>

    <div class="panel-main">
      <header class="panel-header">
        <div style="font-size:15px;font-weight:600">{{ pageTitle }}</div>
        <div style="display:flex;align-items:center;gap:10px">
          <span style="color:var(--mc-sub);font-size:12px">{{ online ? 'MC 在线' : 'MC 离线' }}</span>
          <el-button text circle data-test="theme" @click="toggleTheme">{{ isDark ? '🌙' : '☀️' }}</el-button>
        </div>
      </header>
      <div class="panel-body">
        <Overview v-if="page === 'overview'" />
        <HomeConsole v-else-if="page === 'console'" />
        <FilesPage v-else-if="page === 'files'" />
        <Workflow v-else-if="page === 'workflow'" />
        <FramePanel v-else-if="page === 'frame'" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { api, getToken, setToken } from './api'
import Overview from './pages/Overview.vue'
import HomeConsole from './pages/HomeConsole.vue'
import FilesPage from './pages/Files.vue'
import Workflow from './pages/Workflow.vue'
import FramePanel from './pages/FramePanel.vue'

const ready = ref(false)
const busy = ref(false)
const username = ref('')
const password = ref('')
const err = ref('')
const me = reactive({ username: '', role: '' })
const page = ref('overview')
const online = ref(false)

const menus = [
  { label: 'MC 服务器', items: [
    { key: 'overview', label: '概览', icon: '📊' },
    { key: 'console', label: '控制台', icon: '📡' },
  ] },
  { label: 'MC 运维', items: [
    { key: 'files', label: '文件管理', icon: '📁' },
    { key: 'workflow', label: '工作流', icon: '🔀' },
  ] },
  { label: '系统', items: [
    { key: 'frame', label: '框架后台', icon: '🛠' },
  ] },
]

const pageTitle = computed(() => menus.flatMap(g => g.items).find(m => m.key === page.value)?.label || '')
const isDark = computed(() => document.documentElement.classList.contains('dark'))

function switchPage(k) { page.value = k }

function toggleTheme() {
  const dark = !isDark.value
  document.documentElement.classList.toggle('dark', dark)
  localStorage.setItem('mc_panel_theme', dark ? 'dark' : 'light')
}

let statusTimer = null
function pollStatus() {
  api.status().then(d => { online.value = !!(d.ok && d.data && d.data.online) }).catch(() => { online.value = false })
}

async function doLogin() {
  err.value = ''
  if (!username.value || !password.value) { err.value = '请输入用户名和密码'; return }
  busy.value = true
  try {
    const d = await api.login(username.value, password.value)
    if (d.code === 0) {
      setToken(d.data.token)
      me.username = d.data.username
      me.role = d.data.role || ''
      ready.value = true
      pollStatus()
    } else {
      err.value = d.msg || '登录失败'
    }
  } catch (e) {
    err.value = e.message || '登录失败'
  } finally { busy.value = false }
}

async function doLogout() {
  await api.logout()
  me.username = ''
  ready.value = false
}

onMounted(async () => {
  // 1) Cookie 免登；2) 本地 token
  try {
    const d = await api.me(true)
    if (d.code === 0) {
      me.username = d.data.username; me.role = d.data.role || ''
      ready.value = true; pollStatus(); statusTimer = setInterval(pollStatus, 4000)
      return
    }
  } catch (e) {}
  if (getToken()) {
    try {
      const d = await api.me(false)
      if (d.code === 0) {
        me.username = d.data.username; me.role = d.data.role || ''
        ready.value = true; pollStatus(); statusTimer = setInterval(pollStatus, 4000)
        return
      }
    } catch (e) { setToken('') }
  }
  ready.value = false
})

watch(page, (p) => { if (p === 'console') pollStatus() })
onUnmounted(() => { if (statusTimer) clearInterval(statusTimer) })
</script>

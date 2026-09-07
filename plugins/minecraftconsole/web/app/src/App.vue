<script setup>
import { ref, reactive, computed, onMounted } from 'vue';

const user = ref(null);
const authed = ref(false);
const username = ref('admin');
const password = ref('');
const loginErr = ref('');
const loggingIn = ref(false);
const cur = ref('mc');
const adminGroups = reactive([
  { k: 'basic', title: '基础管理', open: false, pages: [
    { k: 'dashboard', title: '仪表盘', href: './#/dashboard' },
    { k: 'users', title: '用户管理', href: './#/users' },
    { k: 'groups', title: '群组管理', href: './#/groups' },
  ]},
  { k: 'ops', title: '运维管理', open: false, pages: [
    { k: 'commands', title: '命令管理', href: './#/commands' },
    { k: 'logs', title: '日志', href: './#/logs' },
    { k: 'database', title: '数据库', href: './#/database' },
    { k: 'plugins', title: '插件管理', href: './#/plugins' },
    { k: 'settings', title: '设置', href: './#/settings' },
  ]},
]);
const TITLE = {
  mc: ['MC 控制台', '服务器状态 / 快捷命令 / 实时日志'],
  wf: ['MC 工作流', '可视化自动化流程编排'],
  fs: ['文件管理', '服务器文件浏览与编辑'],
};
const curTitle = computed(() => TITLE[cur.value] ? TITLE[cur.value][0] : (cur.value.startsWith('admin:') ? '框架后台' : ''));
const curCrumb = computed(() => TITLE[cur.value] ? TITLE[cur.value][1] : '');
const frameSrc = ref('index.html');
const frameKey = ref(1);

function frameOf(u) { frameSrc.value = u; frameKey.value++; }
function go(k) {
  cur.value = k;
  frameOf(k === 'mc' ? 'index.html' : 'workflow.html' + (k === 'wf' ? '#/workflow' : '#/files'));
}
function toggleGroup(g) { g.open = !g.open; }
function goAdmin(k) {
  cur.value = 'admin:' + k;
  let href = './#/dashboard';
  adminGroups.forEach(g => g.pages.forEach(p => { if (p.k === k) href = p.href; }));
  frameOf(href);
}
async function fetchMe() {
  try {
    const r = await fetch('/api/mc/auth/me');
    if (r.ok) { const j = await r.json(); user.value = j && j.data ? j.data : null; authed.value = true; }
    else { user.value = null; authed.value = false; }
  } catch (e) { user.value = null; authed.value = false; }
}
async function login() {
  if (!username.value || !password.value) { loginErr.value = '请输入用户名和密码'; return; }
  loggingIn.value = true; loginErr.value = '';
  try {
    const r = await fetch('/api/mc/auth/login', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value, password: password.value }) });
    const j = await r.json();
    if (r.ok && j.code === 0) { authed.value = true; user.value = j.data || null; password.value = ''; }
    else { loginErr.value = (j && j.msg) || '登录失败'; }
  } catch (e) { loginErr.value = '登录请求失败'; }
  loggingIn.value = false;
}
async function logout() {
  try { await fetch('/api/mc/auth/logout', { method: 'POST' }); } catch (e) {}
  user.value = null; authed.value = false;
}
onMounted(fetchMe);
</script>

<template>
  <!-- 登录页 -->
  <div class="login" v-if="!authed">
    <div class="card">
      <div class="lg">⛏</div>
      <h1>minecraftconsole</h1>
      <div class="sub">登录后进入 MC 控制台</div>
      <label>用户名</label>
      <input v-model="username" @keyup.enter="login" placeholder="admin">
      <label>密码</label>
      <input v-model="password" type="password" @keyup.enter="login" placeholder="••••••••">
      <div class="err">{{ loginErr }}</div>
      <button :disabled="loggingIn" @click="login">{{ loggingIn ? '登录中…' : '登 录' }}</button>
    </div>
  </div>

  <!-- 主界面 -->
  <div class="app" v-else>
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">⛏</div>
        <div><div class="nm">minecraftconsole</div><div class="sub">MC 控制台</div></div>
      </div>
      <nav class="nav">
        <div class="grp">MC 控制台</div>
        <div class="item" :class="{ active: cur == 'mc' }" @click="go('mc')"><span class="ic">📡</span>控制台</div>
        <div class="item" :class="{ active: cur == 'wf' }" @click="go('wf')"><span class="ic">🔀</span>工作流</div>
        <div class="item" :class="{ active: cur == 'fs' }" @click="go('fs')"><span class="ic">📁</span>文件管理</div>
        <div class="grp">框架后台</div>
        <div v-for="g in adminGroups" :key="g.k">
          <div class="item" @click="toggleGroup(g)"><span class="ic">🛠</span>{{ g.title }}<span class="chev">{{ g.open ? '▾' : '▸' }}</span></div>
          <div v-if="g.open">
            <div v-for="p in g.pages" :key="p.k" class="subitem"
                 :class="{ active: cur == 'admin:' + p.k }" @click="goAdmin(p.k)">{{ p.title }}</div>
          </div>
        </div>
      </nav>
      <div class="foot">© minecraftconsole · FRP MC 管理</div>
    </aside>

    <main class="main">
      <div class="topbar">
        <div class="title">{{ curTitle }}</div>
        <div class="crumb">{{ curCrumb }}</div>
        <div class="spacer"></div>
        <div class="user">
          <span v-if="user">{{ user.username }}</span>
          <button class="lo" @click="logout">登出</button>
        </div>
      </div>
      <div class="content">
        <iframe :src="frameSrc" :key="frameKey"></iframe>
      </div>
    </main>
  </div>
</template>

<style>
:root{
  --bg:#0f1115; --sidebar:#171a21; --sidebar-hover:#1f2430; --border:#262b36;
  --text:#e6e9ef; --muted:#8b93a7; --accent:#2f6bff; --accent-soft:rgba(47,107,255,.12);
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  background:var(--bg);color:var(--text);overflow:hidden}
.app{display:flex;height:100vh}
.sidebar{width:220px;background:var(--sidebar);border-right:1px solid var(--border);
  display:flex;flex-direction:column;flex-shrink:0}
.brand{display:flex;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid var(--border)}
.brand .logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#2f6bff,#6a5cff);
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:17px}
.brand .nm{font-size:14px;font-weight:700}
.brand .sub{font-size:11px;color:var(--muted)}
.nav{flex:1;overflow-y:auto;padding:10px 8px}
.nav .grp{font-size:11px;color:var(--muted);padding:14px 12px 6px;text-transform:uppercase;letter-spacing:.08em}
.nav .item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;
  color:var(--text);font-size:13.5px;transition:background .15s}
.nav .item:hover{background:var(--sidebar-hover)}
.nav .item.active{background:var(--accent-soft);color:#7ea2ff}
.nav .item .ic{width:20px;text-align:center}
.nav .item .chev{margin-left:auto;color:var(--muted);font-size:11px}
.nav .subitem{padding:8px 12px 8px 40px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--muted)}
.nav .subitem:hover{background:var(--sidebar-hover);color:var(--text)}
.nav .subitem.active{background:var(--accent-soft);color:#7ea2ff}
.foot{padding:12px 18px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{height:52px;background:var(--sidebar);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:12px;padding:0 20px}
.topbar .title{font-size:14px;font-weight:600}
.topbar .crumb{color:var(--muted);font-size:12px}
.topbar .spacer{flex:1}
.user{display:flex;align-items:center;gap:8px;font-size:13px}
.user .lo{background:transparent;border:1px solid var(--border);color:var(--muted);
  border-radius:6px;padding:4px 10px;cursor:pointer}
.content{flex:1;min-height:0}
.content iframe{width:100%;height:100%;border:0;background:var(--bg)}
/* 登录页 */
.login{height:100vh;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(ellipse 60% 45% at 50% 0%,#1a2133 0%,var(--bg) 65%)}
.login .card{width:360px;background:var(--sidebar);border:1px solid var(--border);border-radius:14px;padding:28px}
.login .lg{width:52px;height:52px;border-radius:13px;background:linear-gradient(135deg,#2f6bff,#6a5cff);
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:24px;margin-bottom:14px}
.login h1{font-size:18px;font-weight:700;margin-bottom:4px}
.login .sub{font-size:12px;color:var(--muted);margin-bottom:20px}
.login label{display:block;font-size:12px;color:var(--muted);margin:12px 0 6px}
.login input{width:100%;background:#0c0f15;border:1px solid var(--border);color:var(--text);
  border-radius:8px;padding:10px 12px;font-size:14px;outline:none}
.login input:focus{border-color:var(--accent)}
.login .err{color:#f87171;font-size:12px;margin-top:10px;min-height:16px}
.login button{width:100%;margin-top:14px;background:linear-gradient(135deg,#2f6bff,#4f5bff);color:#fff;
  border:0;border-radius:8px;padding:11px;font-size:14px;font-weight:600;cursor:pointer}
.login button:disabled{opacity:.6;cursor:not-allowed}
</style>

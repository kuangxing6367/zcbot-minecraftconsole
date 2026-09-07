// 与框架(8080)与 MC 控制台服务(25598)通信的统一封装
export const MC_PORT = 25598

let _token = localStorage.getItem('mc_token') || localStorage.getItem('zcbot_token') || ''

export function getToken() { return _token }
export function setToken(t) {
  _token = t || ''
  if (t) {
    localStorage.setItem('mc_token', t)
    localStorage.setItem('zcbot_token', t) // 框架 SPA 用同一键恢复会话
  } else {
    localStorage.removeItem('mc_token')
    localStorage.removeItem('zcbot_token')
  }
}

async function request(path, { method = 'GET', body, mcPort = false, noAuth = false } = {}) {
  const headers = {}
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json'
  // 25598 不做鉴权且 CORS 白名单不含 Authorization → 跨源不带
  if (_token && !mcPort && !noAuth) headers['Authorization'] = 'Bearer ' + _token
  const base = mcPort ? `http://${location.hostname}:${MC_PORT}` : ''
  let resp
  try {
    resp = await fetch(base + path, {
      method, headers,
      body: isForm ? body : (body !== undefined ? JSON.stringify(body) : undefined),
    })
  } catch (e) {
    throw new Error('网络错误')
  }
  if (resp.status === 401) {
    if (!noAuth) { setToken(''); await logoutQuiet(); }
    throw new Error('未登录')
  }
  try {
    return await resp.json()
  } catch (e) {
    throw new Error('HTTP ' + resp.status)
  }
}

export async function logoutQuiet() {
  try {
    await fetch('/api/mc/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
  } catch (e) { /* ignore */ }
}

// 本地文件服务（8080，直接操作 mc-server 目录）
export const lfs = {
  list: (rel) => request('/api/mc/lfs/list?path=' + encodeURIComponent(rel || '')),
  read: (rel) => request('/api/mc/lfs/read?path=' + encodeURIComponent(rel || '')),
  write: (path, content, encoding = 'utf-8') => request('/api/mc/lfs/write', { method: 'POST', body: { path, content, encoding } }),
  mkdir: (path) => request('/api/mc/lfs/mkdir', { method: 'POST', body: { path } }),
  rename: (path, newname) => request('/api/mc/lfs/rename', { method: 'POST', body: { path, newname } }),
  del: (path) => request('/api/mc/lfs/delete', { method: 'POST', body: { path } }),
  upload: (dir, files) => {
    const fd = new FormData()
    fd.append('dir', dir || '.')
    files.forEach(f => fd.append('files', f))
    return request('/api/mc/lfs/upload', { method: 'POST', body: fd })
  },
  async download(rel) {
    const resp = await fetch('/api/mc/lfs/download?path=' + encodeURIComponent(rel), {
      headers: _token ? { Authorization: 'Bearer ' + _token } : {},
    })
    if (!resp.ok) throw new Error('下载失败 HTTP ' + resp.status)
    const blob = await resp.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = rel.split('/').pop() || 'download'
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(a.href), 3000)
  },
}

export const api = {
  // 框架侧（8080）
  login: (username, password) => request('/api/mc/auth/login', { method: 'POST', body: { username, password } }),
  me: (noAuth = false) => request('/api/mc/auth/me', { noAuth }),
  logout: async () => { await logoutQuiet(); setToken('') },
  // MC 控制台服务（25598）
  status: () => request('/api/status', { mcPort: true }),
  cmd: (command) => request('/api/cmd', { method: 'POST', body: { command }, mcPort: true }),
  heartbeat: () => request('/api/heartbeat', { mcPort: true }),
  // 文件（8080 FS 代理）
  fsList: (path) => request('/api/mc/fs', { method: 'POST', body: { op: 'list', path } }),
  fsRead: (path, maxBytes = 262144) => request('/api/mc/fs', { method: 'POST', body: { op: 'read', path, maxBytes } }),
}

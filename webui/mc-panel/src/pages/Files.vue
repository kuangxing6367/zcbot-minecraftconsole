<template>
  <div class="mc-card" style="display:flex;overflow:hidden;height:calc(100vh - 160px)">
    <!-- 左：目录树 -->
    <div class="fs-dir-tree" style="width:230px">
      <div class="mc-sub" style="padding:12px 14px 6px">目录</div>
      <el-tree ref="treeRef" :data="treeData" node-key="path" :props="{ label: 'name', children: 'children' }"
               :expand-on-click-node="false" highlight-current @node-click="onTreeClick">
        <template #default="{ data }">
          <span style="display:inline-flex;align-items:center;gap:6px;font-size:13px">
            <span>📁</span><span>{{ data.name }}</span>
          </span>
        </template>
      </el-tree>
    </div>

    <!-- 右：文件区 -->
    <div style="flex:1;min-width:0;display:flex;flex-direction:column;padding:14px 16px">
      <!-- 工具栏 -->
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item>
            <a style="cursor:pointer;color:var(--el-color-primary)" @click="goRoot">根目录</a>
          </el-breadcrumb-item>
          <el-breadcrumb-item v-for="(seg, i) in segs" :key="i">
            <a v-if="i < segs.length - 1" style="cursor:pointer" @click="goTo(segs.slice(0, i + 1).join('/'))">{{ seg }}</a>
            <span v-else>{{ seg }}</span>
          </el-breadcrumb-item>
        </el-breadcrumb>
        <div style="flex:1"></div>
        <el-button size="small" :disabled="!path" data-test="fs-up" @click="goUp">上级</el-button>
        <el-button size="small" @click="openUpload">⬆ 上传</el-button>
        <el-button size="small" @click="askMkdir">📁 新建文件夹</el-button>
        <el-button size="small" :loading="loading" data-test="fs-refresh" @click="load(path)">刷新</el-button>
        <input ref="fileInput" type="file" multiple style="display:none" @change="doUpload" />
      </div>

      <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" style="margin-bottom:10px" />

      <el-table :data="sortedRows" style="width:100%" highlight-current-row v-loading="loading"
                @row-dblclick="onOpen" @sort-change="onSort">
        <el-table-column label="名称" min-width="240" prop="name" sortable="custom">
          <template #default="{ row }">
            <div class="file-row-name">
              <span style="font-size:16px">{{ row.is_dir ? '📁' : iconOf(row.name) }}</span>
              <span :class="{ mono: !row.is_dir }" style="cursor:pointer" @click="onOpen(row)">{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100" align="right" prop="size" sortable="custom">
          <template #default="{ row }">{{ row.is_dir ? '-' : fmt(row.size) }}</template>
        </el-table-column>
        <el-table-column label="修改时间" width="160" prop="mtime" sortable="custom">
          <template #default="{ row }"><span style="color:var(--mc-sub)">{{ row.mtime }}</span></template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">{{ row.is_dir ? '目录' : extOf(row.name) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.is_dir" link type="primary" size="small" @click="onOpen(row)">进入</el-button>
            <template v-else>
              <el-button link type="primary" size="small" @click="editFile(row)">编辑</el-button>
              <el-button link type="primary" size="small" @click="download(row)">下载</el-button>
            </template>
            <el-button link type="warning" size="small" @click="rename(row)">重命名</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 文件查看/编辑 -->
    <el-dialog v-model="dlg" :title="dlgTitle" width="72%" top="5vh" destroy-on-close>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <span class="mono mc-sub" data-test="editor-path">{{ dlgPath }}<template v-if="enc"> · {{ enc }}</template></span>
        <span style="display:flex;align-items:center;gap:10px">
          <el-select v-if="canEdit" v-model="enc" size="small" style="width:100px">
            <el-option label="UTF-8" value="utf-8" />
            <el-option label="GBK" value="gbk" />
          </el-select>
          <el-switch v-if="canEdit" v-model="editMode" active-text="编辑" inactive-text="只读" />
        </span>
      </div>
      <el-input v-model="content" type="textarea" :rows="20" data-test="editor"
                :readonly="!editMode || !canEdit" style="font-family:Consolas,Menlo,monospace;font-size:13px" />
      <template #footer>
        <el-button @click="dlg = false">关闭</el-button>
        <el-button type="primary" :disabled="!editMode || !canEdit" :loading="saving" data-test="editor-save"
                   @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新建文件夹 -->
    <el-dialog v-model="mkDlg" title="新建文件夹" width="420px">
      <el-input v-model="mkName" placeholder="文件夹名称" data-test="mkdir-name" @keyup.enter="doMkdir" />
      <template #footer>
        <el-button @click="mkDlg = false">取消</el-button>
        <el-button type="primary" :loading="busy" data-test="mkdir-btn" @click="doMkdir">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { lfs } from '../api'

const path = ref('')
const rows = ref([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const treeData = ref([])
const treeRef = ref(null)
const fileInput = ref(null)

// 编辑器
const dlg = ref(false)
const dlgTitle = ref('查看文件')
const dlgPath = ref('')
const content = ref('')
const editMode = ref(false)
const canEdit = ref(false)
const enc = ref('utf-8')
const saving = ref(false)
const editingRel = ref('')

const mkDlg = ref(false)
const mkName = ref('')

const segs = computed(() => path.value ? path.value.split('/') : [])

// 排序：目录永远在前，再按当前列排（默认名称升序）
const sortState = { key: 'name', order: 'asc' }
const sortedRows = computed(() => {
  const arr = [...rows.value]
  arr.sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    let r = 0
    if (sortState.key === 'size') r = (a.size || 0) - (b.size || 0)
    else if (sortState.key === 'mtime') r = String(a.mtime).localeCompare(String(b.mtime))
    else r = String(a.name).localeCompare(String(b.name), 'zh')
    return sortState.order === 'asc' ? r : -r
  })
  return arr
})
function onSort({ prop, order }) { if (prop && order) { sortState.key = prop; sortState.order = order } }

const TEXT_EXT = ['TXT', 'LOG', 'JSON', 'YML', 'YAML', 'PROPERTIES', 'CFG', 'XML', 'MD', 'INI', 'CONF', 'BAT', 'CMD', 'SH', 'TXT']
function fmt(n) {
  if (n == null || isNaN(n)) return '-'
  if (n < 1024) return n + ' B'
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1048576).toFixed(1) + ' MB'
}
const extOf = (n) => (n.includes('.') ? n.split('.').pop().toUpperCase() : '文件')
const iconOf = (n) => (['PNG', 'JPG', 'JPEG', 'GIF', 'ICO', 'WEBP'].includes(extOf(n)) ? '🖼'
  : ['JSON', 'YML', 'YAML', 'PROPERTIES', 'TXT', 'LOG', 'CFG', 'XML', 'MD'].includes(extOf(n)) ? '📄'
  : extOf(n) === 'JAR' ? '📦' : '📄')

const textLike = (name, size) => (TEXT_EXT.includes(extOf(name)) || extOf(name) === '文件') && (size || 0) <= 2 * 1024 * 1024

function sel(node) { if (treeRef.value) treeRef.value.setCurrentKey(node || '') }

async function load(dir) {
  if (dir !== undefined) path.value = String(dir).replace(/^\/+/, '')
  loading.value = true
  error.value = ''
  try {
    const d = await lfs.list(path.value)
    if (d.code === 0) { rows.value = d.data.rows }
    else if (d.code !== 401) { error.value = d.msg || '加载失败'; rows.value = [] }
  } catch (e) { error.value = '请求失败: ' + e.message }
  finally { loading.value = false }
}

async function dirsOf(rel) {
  try {
    const d = await lfs.list(rel || '')
    return (d.code === 0 ? d.data.rows : []).filter(r => r.is_dir)
  } catch (e) { return [] }
}

async function buildTree() {
  const dirs = await dirsOf('')
  treeData.value = dirs.map(dd => ({ name: dd.name, path: dd.rel, children: [] }))
}

async function onTreeClick(data) {
  if (data.children && data.children.length === 0) {
    const subs = await dirsOf(data.path)
    data.children = subs.map(s => ({ name: s.name, path: s.rel, children: [] }))
  }
  load(data.path)
}

const goRoot = () => { sel(''); load('') }
const goUp = () => {
  const parts = path.value.split('/').filter(Boolean); parts.pop()
  sel(parts.join('/')); load(parts.join('/'))
}
const goTo = (p) => { sel(p); load(p) }

const onOpen = (row) => { if (row.is_dir) goTo(row.rel); else editFile(row) }

// ---- 编辑/查看 ----
async function editFile(row) {
  editingRel.value = row.rel
  dlgPath.value = row.rel
  dlgTitle.value = row.name
  canEdit.value = false
  dlg.value = true
  content.value = '加载中…'
  editMode.value = false
  try {
    const d = await lfs.read(row.rel)
    if (d.code === 0) {
      if (d.data.binary) {
        content.value = `二进制文件（${fmt(d.data.size)}），不支持在线编辑。请用「下载」取回本地处理。`
      } else {
        canEdit.value = textLike(row.name, d.data.size)
        enc.value = d.data.encoding === 'gbk' ? 'gbk' : 'utf-8'
        content.value = d.data.content
      }
    } else {
      content.value = '读取失败: ' + (d.msg || '')
    }
  } catch (e) { content.value = '请求失败: ' + e.message }
}

async function save() {
  saving.value = true
  try {
    const d = await lfs.write(editingRel.value, content.value, enc.value)
    ElMessage.success(d.code === 0 ? '已保存（' + enc.value + '）' : (d.msg || '保存失败'))
  } catch (e) { ElMessage.error('保存失败: ' + e.message) }
  finally { saving.value = false }
}

// ---- 上传 ----
function openUpload() { fileInput.value && fileInput.value.click() }
async function doUpload(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (!files.length) return
  try {
    const d = await lfs.upload(path.value || '.', files)
    ElMessage.success(d.code === 0 ? '已上传' : (d.msg || '上传失败'))
    load(path.value)
  } catch (err) { ElMessage.error('上传失败: ' + err.message) }
}

// ---- 下载 ----
async function download(row) {
  try { await lfs.download(row.rel) }
  catch (e) { ElMessage.error(e.message) }
}

// ---- 重命名 ----
async function rename(row) {
  const oldName = row.name
  const { value } = await ElMessageBox.prompt(`为「${oldName}」输入新名称`, '重命名', {
    inputValue: oldName, confirmButtonText: '确定', cancelButtonText: '取消',
  })
  if (value == null || value === oldName) return
  try {
    const d = await lfs.rename(row.rel, value)
    ElMessage.success(d.code === 0 ? '已重命名' : (d.msg || '失败'))
    load(path.value)
  } catch (e) { ElMessage.error('重命名失败: ' + e.message) }
}

// ---- 删除 ----
async function remove(row) {
  try {
    await ElMessageBox.confirm(`确定删除「${row.name}」${row.is_dir ? '（整目录）' : ''}？此操作不可恢复。`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch (e) { return }
  try {
    const d = await lfs.del(row.rel)
    ElMessage.success(d.code === 0 ? '已删除' : (d.msg || '失败'))
    if (row.is_dir && path.value.startsWith(row.rel)) { goUp() } else load(path.value)
  } catch (e) { ElMessage.error('删除失败: ' + e.message) }
}

// ---- 新建文件夹 ----
function askMkdir() { mkName.value = ''; mkDlg.value = true }
async function doMkdir() {
  const name = mkName.value.trim()
  if (!name) { ElMessage.warning('请输入名称'); return }
  busy.value = true
  try {
    const rel = path.value ? path.value + '/' + name : name
    const d = await lfs.mkdir(rel)
    if (d.code === 0) { ElMessage.success('已创建'); mkDlg.value = false; load(path.value) }
    else ElMessage.error(d.msg || '创建失败')
  } catch (e) { ElMessage.error('创建失败: ' + e.message) }
  finally { busy.value = false }
}

onMounted(() => { buildTree(); load('') })
</script>

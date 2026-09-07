<template>
  <!-- 框架后台 = 原框架管理面板（群组/用户/权限/接口令牌/文件管理），同源 iframe。
       外层高度与滚动容器对齐，杜绝页脚/双滚动条溢出。 -->
  <div style="height:calc(100vh - 112px);overflow:hidden;background:var(--mc-panel);
              border:1px solid var(--mc-border);border-radius:8px">
    <iframe :src="src" style="width:100%;height:100%;border:0;display:block" />
  </div>
</template>

<script setup>
import { ref } from 'vue'
const src = ref('/index.html')
</script>

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // 输出到 插件 web/panel：从 mc-panel 向上两级到项目根，再进 plugins/minecraftconsole/web/panel
    outDir: fileURLToPath(new URL('../../plugins/minecraftconsole/web/panel', import.meta.url)),
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 2500,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name].[hash].js',
        chunkFileNames: 'assets/[name].[hash].js',
        assetFileNames: 'assets/[name].[hash][extname]',
        manualChunks: {
          'el': ['element-plus', '@element-plus/icons-vue'],
          'echarts': ['echarts'],
          'vue-vendor': ['vue'],
        },
      },
    },
  },
})

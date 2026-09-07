import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'
import './style.css'

// 默认浅色（1Panel 风格）；右上角可切深色
if (localStorage.getItem('mc_panel_theme') === 'dark') {
  document.documentElement.classList.add('dark')
}

const app = createApp(App)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')

import { createRoot } from 'react-dom/client'
import App from './App'
import { SharedPage } from './components/Library'
import { SharedSkill } from './components/Routines'
import { SharedTemplate } from './components/Templates'
import './styles.css'
import 'highlight.js/styles/github-dark.css'

// /s/<token> 是分享出去的页面，/skill/<token> 是技能，/template/<token> 是 Bot 模板：只显示它本身，不加载应用
const page = location.pathname.match(/^\/s\/([\w-]+)$/)
const skill = location.pathname.match(/^\/skill\/([\w-]+)$/)
const template = location.pathname.match(/^\/template\/([\w-]+)$/)
const shared = page || skill || template
createRoot(document.getElementById('root')!).render(
  page ? <SharedPage token={page[1]} /> : skill ? <SharedSkill token={skill[1]} />
    : template ? <SharedTemplate token={template[1]} /> : <App />)

// Service Worker：添加到主屏幕、推送通知。只在 https 或本机地址下可用
if (!shared && 'serviceWorker' in navigator && isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {})

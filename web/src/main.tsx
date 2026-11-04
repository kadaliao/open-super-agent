import { createRoot } from 'react-dom/client'
import App from './App'
import { SharedPage } from './components/Library'
import './styles.css'
import 'highlight.js/styles/github-dark.css'

// /s/<token> 是分享出去的页面：只显示页面本身，不加载应用
const shared = location.pathname.match(/^\/s\/([\w-]+)$/)
createRoot(document.getElementById('root')!).render(shared ? <SharedPage token={shared[1]} /> : <App />)

// Service Worker：添加到主屏幕、推送通知。只在 https 或本机地址下可用
if (!shared && 'serviceWorker' in navigator && isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {})

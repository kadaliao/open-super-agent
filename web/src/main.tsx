import { createRoot } from 'react-dom/client'
import App from './App'
import { SharedPage } from './components/Library'
import './styles.css'
import 'highlight.js/styles/github-dark.css'

// /s/<token> 是分享出去的页面：只显示页面本身，不加载应用
const shared = location.pathname.match(/^\/s\/([\w-]+)$/)
createRoot(document.getElementById('root')!).render(shared ? <SharedPage token={shared[1]} /> : <App />)

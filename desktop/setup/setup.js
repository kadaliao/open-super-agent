// App 自带的启动页：第一次打开时选用法；在这台电脑上运行时，显示服务启动的进度和出错的原因。
const invoke = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args)

const TEXT = {
  zh: {
    choose: '选一种用法，以后可以在菜单栏里切换',
    localTitle: '在这台电脑上运行', localBtn: '开始',
    localDesc: 'App 自带运行环境，不用装 Docker 和 Python。数据存在这台电脑上；关掉窗口后在菜单栏继续运行，定时任务和聊天软件照常工作。',
    remoteTitle: '连接到我的服务器', remoteBtn: '连接',
    remoteDesc: '已经在服务器上部署了 open-super-agent？填它的地址，在 App 里登录。',
    back: '取消',
    starting: '正在启动…第一次要十几秒',
    failed: '服务没能启动', retry: '重试', other: '换一种用法', logs: '打开日志文件夹',
  },
  en: {
    choose: 'Choose how to use it. You can switch later from the menu bar.',
    localTitle: 'Run on this computer', localBtn: 'Start',
    localDesc: "Everything it needs is built in: no Docker or Python to install. Your data stays on this computer, and it keeps running in the menu bar after you close the window, so routines and chat apps keep working.",
    remoteTitle: 'Connect to my server', remoteBtn: 'Connect',
    remoteDesc: 'Already running open-super-agent on a server? Enter its address and sign in inside the app.',
    back: 'Cancel',
    starting: 'Starting… the first time takes a few seconds',
    failed: "The service didn't start", retry: 'Try again', other: 'Use it another way', logs: 'Open logs folder',
  },
}

const $ = id => document.getElementById(id)
const show = which => ['choose', 'starting', 'failed'].forEach(id => { $(id).hidden = id !== which })
const switching = new URLSearchParams(location.search).has('switch')
let state = null, poll = null

async function refresh() {
  state = await invoke('setup_state')
  const t = TEXT[state.zh ? 'zh' : 'en']
  document.documentElement.lang = state.zh ? 'zh-CN' : 'en'
  document.querySelectorAll('[data-t]').forEach(el => { el.textContent = t[el.dataset.t] })
  if (state.url && !$('url').value) $('url').value = state.url
  $('back').hidden = !(switching && state.mode)
  if (state.phase === 'starting') show('starting')
  else if (state.phase === 'error') { $('message').textContent = state.message; show('failed') }
  else if (switching || !state.mode) show('choose')
  else show('starting')   // 正在打开服务器，马上就跳走
  clearTimeout(poll)
  if (state.phase === 'starting' || (!switching && state.mode)) poll = setTimeout(refresh, 700)
}

$('local').onclick = async () => { await invoke('choose_local'); refresh() }
$('remote').onsubmit = async e => {
  e.preventDefault()
  const btn = e.submitter, err = $('remoteErr')
  btn.disabled = true; err.hidden = true
  try { await invoke('choose_remote', { url: $('url').value }) } catch (x) { err.textContent = String(x); err.hidden = false }
  btn.disabled = false
}
$('back').onclick = () => state.mode === 'local' ? invoke('choose_local') : invoke('choose_remote', { url: state.url })
$('retry').onclick = async () => { await invoke('choose_local'); refresh() }
$('other').onclick = () => show('choose')
$('logs').onclick = () => invoke('open_logs')

refresh()

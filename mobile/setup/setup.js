// App 自带的连接页：第一次打开时填服务器地址（或者用相机扫电脑上的二维码）；扫到别的服务器的二维码时先确认。
const invoke = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args)

const TEXT = {
  zh: {
    sub: '连接你部署的 open-super-agent 服务器',
    scanTitle: '扫码连接（推荐）',
    scan1: '在电脑上打开它的网页，进入 设置 → 手机 App',
    scan2: '用 iPhone 的相机扫屏幕上的二维码，点“在 Open Super Agent 中打开”',
    urlTitle: '或者填地址', urlDesc: '手机要能打开这个地址：局域网 IP、Tailscale 或域名都行。之后在服务器的登录页输入密码。',
    connectBtn: '连接', back: '取消',
    pairTitle: '连接到这台服务器？', pairDesc: '扫到的二维码指向另一台服务器。确认是你自己的服务器再连接。',
    pairOk: '连接', pairNo: '不连接', opening: '正在打开…',
  },
  en: {
    sub: 'Connect to your open-super-agent server',
    scanTitle: 'Scan to connect (recommended)',
    scan1: 'On your computer, open its web page and go to Settings → Phone app',
    scan2: 'Scan the QR code with the iPhone Camera and tap “Open in Open Super Agent”',
    urlTitle: 'Or enter the address', urlDesc: 'Your phone needs to reach it: a LAN IP, Tailscale or a domain all work. Then sign in on the server’s login page.',
    connectBtn: 'Connect', back: 'Cancel',
    pairTitle: 'Connect to this server?', pairDesc: 'The QR code points to a different server. Only connect if it’s yours.',
    pairOk: 'Connect', pairNo: 'Don’t connect', opening: 'Opening…',
  },
}

const $ = id => document.getElementById(id)
const show = which => ['connect', 'pair', 'opening'].forEach(id => { $(id).hidden = id !== which })
const q = new URLSearchParams(location.search)
let state = null

async function refresh() {
  state = await invoke('setup_state')
  const t = TEXT[state.zh ? 'zh' : 'en']
  document.documentElement.lang = state.zh ? 'zh-CN' : 'en'
  document.querySelectorAll('[data-t]').forEach(el => { el.textContent = t[el.dataset.t] })
  if (state.url && !$('url').value) $('url').value = state.url
  $('err').hidden = !state.error; $('err').textContent = state.error || ''
  $('back').hidden = !(q.has('switch') && state.url)
  if (state.pair) { $('pairServer').textContent = state.pair; show('pair') }
  else if (state.url && !q.has('switch') && !state.error) show('opening')   // 正在打开上次的服务器，马上就跳走
  else show('connect')
}

$('remote').onsubmit = async e => {
  e.preventDefault()
  const btn = e.submitter, err = $('err')
  btn.disabled = true; err.hidden = true
  try { await invoke('choose_remote', { url: $('url').value }) } catch (x) { err.textContent = String(x); err.hidden = false }
  btn.disabled = false
}
$('back').onclick = () => invoke('cancel_pair')
$('pairOk').onclick = async () => {
  $('pairOk').disabled = true
  try { await invoke('confirm_pair') } catch (x) { $('pairErr').textContent = String(x); $('pairErr').hidden = false }
  $('pairOk').disabled = false
}
$('pairNo').onclick = () => invoke('cancel_pair').then(refresh)

refresh()

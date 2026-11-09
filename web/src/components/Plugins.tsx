import { useEffect, useState } from 'react'
import { api } from '../store'
import { t, tn } from '../i18n'
import { ask } from './Dialog'
import { Section, useToast } from './settingsKit'

// 设置 → 高级 → 开发者插件（预览）：data/plugins 里的 Python 代码（osa/plugins.py）。启用要用户自己点，启用时记下文件指纹，
// 文件改过就不加载，要再看一遍、再启用。插件市场是随项目发布、审查过的插件（osa/catalog/plugins）。
const DOC = <a href="https://github.com/kadaliao/open-super-agent/blob/main/docs/plugins.md" target="_blank" rel="noreferrer">docs/plugins.md</a>

type Plugin = {
  name: string; title: string; description: string; version: string; author: string; kind: 'file' | 'folder'
  enabled: boolean; loaded: boolean; changed: boolean; error: string; tools: string[]
  config: Record<string, unknown>; default_config: Record<string, unknown>; from_catalog: boolean; update: boolean
  blocked?: string   // security.plugins 是 market：这个不是插件市场里原样的，不能启用、不加载（原因）
  provide: string[]; inject: string[]; cycle: string[]
  waiting: { service: string; by: string[] }[]           // 启用了、在等的服务，by 是能提供它的插件（core 是内置的）
  dependents: { name: string; loaded: boolean }[]        // 要用它提供的服务的插件
}
type Item = { name: string; title: string; description: string; version: string; author: string; installed: boolean; provide: string[]; inject: string[] }
type Service = { name: string; describe: string }
type Data = { locked: string; dir: string; plugins: Plugin[]; catalog: Item[]; services: Service[] }

// 提供 / 用到的服务，一行写清楚：启用前就能看出它要用模型（花钱）、会往对话里发消息
function Wiring({ provide, inject, core }: { provide: string[]; inject: string[]; core: Service[] }) {
  const desc = (s: string) => core.find(c => c.name === s)?.describe
  if (!provide.length && !inject.length) return null
  return (
    <span className="muted small plugin-wiring">
      {inject.length > 0 && <span>{t('用到：')}{inject.map(s => <code key={s} title={desc(s) || ''}>{s}</code>)}
        {inject.some(desc) && <> （{inject.filter(desc).map(s => desc(s)).join('；')}）</>}</span>}
      {provide.length > 0 && <span>{t('提供：')}{provide.map(s => <code key={s}>{s}</code>)}</span>}
    </span>
  )
}

export default function Plugins() {
  const [d, setD] = useState<Data | null>(null)
  const [err, setErr] = useState('')
  const load = () => api<Data>('/api/plugins').then(setD).catch(e => setErr(e.message))
  useEffect(() => {
    load()
    window.addEventListener('osa-plugins', load)
    return () => window.removeEventListener('osa-plugins', load)
  }, [])
  if (err) return <div className="error-line">{err}</div>
  if (!d) return <div className="muted">{t('加载中…')}</div>
  const updates = new Set(d.plugins.filter(p => p.update).map(p => p.name))
  return (
    <div>
      <p>{tn('先想清楚是不是插件：接外部服务用应用（MCP），教 Bot 做事的方法写成技能，只有要碰 osa 自己的东西（订阅事件、看任务结果、往对话里写消息）才写插件，见 {doc}。', { doc: DOC })}</p>
      <p className="muted">{tn('插件是放在 {dir} 里的 Python 代码，和服务在同一个进程里运行，能做服务能做的一切（读写这台机器上的文件、访问网络）。只启用你看过、信得过的。启用时会记下文件的指纹，文件改过就不再加载，要你看过改动再启用一次。', { dir: <code>{d.dir}</code> })}</p>
      {d.locked && <div className="error-line">{d.locked}</div>}
      <Section title={t('已安装')}>
        {d.plugins.length ? <div className="set-rows">{d.plugins.map(p => <Row key={p.name} p={p} all={d.plugins} core={d.services} locked={!!d.locked} reload={load} />)}</div>
          : <div className="muted small">{t('还没有插件。从下面的插件市场装一个，或者把 .py 文件放进上面的文件夹。')}</div>}
      </Section>
      <Section title={t('插件市场')} desc={t('随项目发布、审查过的插件。装上以后还要你看过代码、自己启用。')}>
        <div className="set-rows">
          {d.catalog.map(c => <CatalogRow key={c.name} c={c} core={d.services} update={updates.has(c.name)} locked={!!d.locked} reload={load} />)}
        </div>
      </Section>
    </div>
  )
}

function Row({ p, all, core, locked, reload }: { p: Plugin; all: Plugin[]; core: Service[]; locked: boolean; reload: () => void }) {
  const toast = useToast()
  const [open, setOpen] = useState<'' | 'source' | 'config'>('')
  const [busy, setBusy] = useState(false)
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true)
    try { await fn(); if (ok) toast(ok) } catch (e: any) { toast(e.message, true) }
    setBusy(false); reload()
  }
  const toggle = async (on: boolean) => {
    if (on && !(await ask(t('启用插件「{name}」？它会在服务里运行，能读写这台机器上的文件、访问网络。确认你看过它的代码。', { name: p.title || p.name }), { ok: t('启用') }))) return
    const running = p.dependents.filter(d => d.loaded).map(d => titleOf(all, d.name))
    if (!on && running.length && !(await ask(t('「{names}」用到它提供的服务，会跟着停下；重新启用它以后会自动恢复。', { names: running.join('」「') }), { ok: t('停用') }))) return
    run(() => api(`/api/plugins/${p.name}/${on ? 'enable' : 'disable'}`, { method: 'POST' }), on ? t('已启用') : t('已停用'))
  }
  const remove = async () => {
    if (!(await ask(t('删除插件「{name}」？文件和它的数据目录都会删掉。', { name: p.title || p.name }), { ok: t('删除'), danger: true }))) return
    run(() => api(`/api/plugins/${p.name}`, { method: 'DELETE' }), t('已删除'))
  }
  const status = p.changed ? <span className="chip warn">{t('文件改过了，看过后重新启用')}</span>
    : p.error ? <span className="chip bad">{t('出错')}</span>
    : p.cycle.length ? <span className="chip bad">{t('循环依赖')}</span>
    : p.waiting.length ? <span className="chip warn">{t('等待依赖')}</span>
    : p.loaded ? <span className="chip ok">{t('运行中')}</span>
    : <span className="chip faint">{p.enabled ? t('没加载') : t('已停用')}</span>
  return (
    <div className="set-row plugin-row">
      <span>
        <b>{p.title || p.name}{p.version && <span className="muted small"> · {p.version}</span>} {status}</b>
        {p.description && <span>{p.description}</span>}
        <span className="muted small"><code>{p.name}</code>{p.kind === 'folder' ? ' · ' + t('文件夹') : ''}
          {p.tools.length > 0 && <> · {t('工具：{names}', { names: p.tools.join('、') })}</>}</span>
        <Wiring provide={p.provide} inject={p.inject} core={core} />
        {p.waiting.map(w => <span key={w.service} className="plugin-wait">{w.by.length
          ? tn('等服务 {s}：先启用 {who}', { s: <code>{w.service}</code>, who: w.by.map(n => `「${titleOf(all, n)}」`).join(t('或')) })
          : tn('等服务 {s}：没有装能提供它的插件', { s: <code>{w.service}</code> })}</span>)}
        {p.cycle.length > 0 && <span className="plugin-wait bad">{t('循环依赖：{path}，这几个永远等不到对方，改掉其中一个的 inject', { path: p.cycle.map(n => titleOf(all, n)).join(' → ') })}</span>}
        {p.blocked && <span className="plugin-wait bad">{p.blocked}</span>}
        {p.error && p.error !== p.blocked && <pre className="plugin-err">{p.error}</pre>}
        <span className="plugin-actions">
          <button className="link-btn" onClick={() => setOpen(open === 'source' ? '' : 'source')}>{open === 'source' ? t('收起代码') : t('查看代码')}</button>
          <button className="link-btn" onClick={() => setOpen(open === 'config' ? '' : 'config')}>{t('设置')}</button>
          {p.enabled && !p.changed && <button className="link-btn" disabled={busy || locked} onClick={() => run(() => api(`/api/plugins/${p.name}/reload`, { method: 'POST' }), t('已重新加载'))}>{t('重新加载')}</button>}
          <button className="link-btn danger" disabled={busy} onClick={remove}>{t('删除')}</button>
        </span>
        {open === 'source' && <Source url={`/api/plugins/${p.name}/source`} />}
        {open === 'config' && <Config p={p} onSaved={reload} />}
      </span>
      <label className="switch" title={p.enabled ? t('停用') : t('启用')}>
        {/* 确认框是异步的：点完开关、在框里决定之前，浏览器已经把勾改了；用 key 让它按服务端的状态重新画 */}
        <input key={String(p.enabled && !p.changed)} type="checkbox" checked={p.enabled && !p.changed} disabled={busy || ((locked || !!p.blocked) && !p.enabled)}
          onChange={e => toggle(e.target.checked)} /><span />
      </label>
    </div>
  )
}

function Source({ url }: { url: string }) {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => { api(url).then(r => setSrc(r.source)).catch(e => setSrc(e.message)) }, [url])
  return <pre className="plugin-src">{src ?? t('加载中…')}</pre>
}

// 插件的设置：一个 JSON 对象，插件里是 ctx.config。密钥写 "vault:名字"，插件用 ctx.secret() 从保险库取
function Config({ p, onSaved }: { p: Plugin; onSaved: () => void }) {
  const toast = useToast()
  const [text, setText] = useState(() => JSON.stringify(p.config, null, 2))
  const save = async () => {
    let config: unknown
    try { config = JSON.parse(text) } catch { toast(t('不是合法的 JSON'), true); return }
    try { await api(`/api/plugins/${p.name}/config`, { method: 'PUT', body: { config } }); toast(t('已保存')); onSaved() }
    catch (e: any) { toast(e.message, true) }
  }
  return (
    <span className="plugin-cfg">
      <textarea rows={Math.min(12, text.split('\n').length + 1)} value={text} spellCheck={false} onChange={e => setText(e.target.value)} />
      <span className="muted small">{tn('密钥不要明文写在这里：先存进保险库，这里写 {ref}。', { ref: <code>{t('vault:名字')}</code> })}</span>
      <span><button className="btn primary" onClick={save}>{t('保存')}</button>
        {Object.keys(p.default_config).length > 0 && <button className="btn" onClick={() => setText(JSON.stringify(p.default_config, null, 2))}>{t('恢复默认')}</button>}</span>
    </span>
  )
}

function CatalogRow({ c, core, update, locked, reload }: { c: Item; core: Service[]; update: boolean; locked: boolean; reload: () => void }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const install = async () => {
    if (update && !(await ask(t('用插件市场里的版本替换「{name}」？替换后要重新启用。', { name: c.title || c.name }), { ok: t('替换') }))) return
    try { await api(`/api/plugins/${c.name}/install`, { method: 'POST' }); toast(t('已安装，看过代码后在上面启用')) }
    catch (e: any) { toast(e.message, true) }
    reload()
  }
  return (
    <div className="set-row plugin-row">
      <span>
        <b>{c.title || c.name}{c.version && <span className="muted small"> · {c.version}</span>}</b>
        {c.description && <span>{c.description}</span>}
        <Wiring provide={c.provide} inject={c.inject} core={core} />
        <span className="plugin-actions"><button className="link-btn" onClick={() => setOpen(!open)}>{open ? t('收起代码') : t('查看代码')}</button></span>
        {open && <Source url={`/api/plugins/${c.name}/source?catalog=1`} />}
      </span>
      {c.installed && !update ? <span className="chip faint">{t('已安装')}</span>
        : <button className="btn" disabled={locked} onClick={install}>{update ? t('更新') : t('安装')}</button>}
    </div>
  )
}

const titleOf = (all: Plugin[], name: string) => (name === 'core' ? t('内置') : all.find(p => p.name === name)?.title || name)

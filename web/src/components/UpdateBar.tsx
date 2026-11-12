import { useState } from 'react'
import { t } from '../i18n'
import { desktop } from '../desktop'
import { reloadForUpdate, useStore } from '../store'

// 有新版本时屏幕顶上的一条。桌面 App 自己的新版装好了：“重启更新”（重启后界面也是新的）；
// 否则是服务器上的网页界面换了新版：“更新”，刷新页面。点 × 这次先不管，换了别的新版本才再提示
export default function UpdateBar() {
  const web = useStore(s => s.newBuild)
  const app = useStore(s => s.appUpdate)
  const [dismissed, setDismissed] = useState('')
  const key = app ? 'app:' + app : web ? 'web' : ''
  if (!key || key === dismissed) return null
  return (
    <div className="update-bar" role="status">
      <span className="update-text">{app ? t('新版本 {version} 已经装好', { version: app }) : t('有新版本了')}</span>
      <button className="btn primary" onClick={() => app ? desktop?.update?.restart() : reloadForUpdate()}>{app ? t('重启更新') : t('更新')}</button>
      <button className="update-x" aria-label={t('稍后')} title={t('稍后')} onClick={() => setDismissed(key)}>×</button>
    </div>
  )
}

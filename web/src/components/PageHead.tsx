// 整页的页面头（看板、观测、应用、资料库）：标题、一句说明、右边的操作；下面还能再放一行（汇总、标签）。
// 样式在 page.css。手机上左边给菜单按钮让位，说明省掉，操作放不下就换到第二行
import type { ReactNode } from 'react'

export default function PageHead({ title, desc, actions, children, className }: {
  title: ReactNode; desc?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string
}) {
  return (
    <header className={'ph' + (className ? ' ' + className : '')}>
      <div className="ph-row">
        <div className="ph-text">
          <h1 className="ph-title">{title}</h1>
          {desc && <p className="ph-desc">{desc}</p>}
        </div>
        {actions && <div className="ph-actions">{actions}</div>}
      </div>
      {children}
    </header>
  )
}

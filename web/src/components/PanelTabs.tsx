import { useEffect, useLayoutEffect, useRef, useState } from 'react'

// 右栏顶上带下划线的一排标签：放不下时横着滑，还有没露出来的标签时那一侧渐隐；切换（包括从别处打开某一页）时把当前项滚进视野
export default function PanelTabs({ tabs, cur, onPick }: { tabs: [string, string][]; cur: string; onPick: (key: string) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState('')
  const measure = () => {
    const el = box.current
    if (!el) return
    const left = el.scrollLeft > 2, right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2
    setMore((left ? ' more-l' : '') + (right ? ' more-r' : ''))
  }
  useLayoutEffect(() => {
    const el = box.current
    const on = el?.querySelector<HTMLElement>('button.on')
    if (el && on) {
      const pad = 24   // 留出渐隐的那一截，当前项不落在半透明里
      if (on.offsetLeft - pad < el.scrollLeft) el.scrollLeft = on.offsetLeft - pad
      else if (on.offsetLeft + on.offsetWidth + pad > el.scrollLeft + el.clientWidth) el.scrollLeft = on.offsetLeft + on.offsetWidth + pad - el.clientWidth
    }
    measure()
  }, [cur])
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return (
    <div className={'panel-tabs line' + more} ref={box} onScroll={measure} role="tablist">
      {tabs.map(([key, label]) => <button key={key} role="tab" aria-selected={key === cur} className={key === cur ? 'on' : ''} onClick={() => onPick(key)}>{label}</button>)}
    </div>
  )
}

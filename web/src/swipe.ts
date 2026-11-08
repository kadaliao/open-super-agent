// 手机上从屏幕左边缘往右滑：回到上一层，和 iOS 的返回手势一样跟手（松手时过了三分之一或者甩得够快就返回，不然弹回去）。
// - 上一层是最后打开、还开着的那一层：弹窗、右栏、资料库里的页面……各自用 useBack 登记。
//   一层都没有时把侧栏（对话列表）拉出来；侧栏开着就到顶了，什么也不做（侧栏上面又开了弹窗的，先关弹窗）。
// - 只在手机布局里、只从左边缘起手；横着动得够多、明显比竖着多才算，不然照常滚动、选字。
// - 不抢：还能往左滚回去的横向滚动区（代码块、表格）、接手时的电脑画面、标了 data-no-swipe 的地方。
import { useEffect, useRef, type RefObject } from 'react'
import { flushSync } from 'react-dom'

type Layer = { back: (() => void) | null; el?: RefObject<HTMLElement | null> }   // back 为空：侧栏，到顶了

const layers: Layer[] = []
let drawer: (() => void) | null = null   // 侧栏关着时：把它拉出来

const EDGE = 24      // 离左边缘多近起手才算
const SLOP = 10      // 动了这么多才判断方向
const MS = 200       // 松手后滑走或弹回的时间
const phone = typeof matchMedia === 'undefined' ? null : matchMedia('(max-width: 760px)')

function push(layer: Layer) {
  layers.push(layer)
  return () => { const i = layers.indexOf(layer); if (i >= 0) layers.splice(i, 1) }
}

/** 登记一层：左边缘右滑时调 back。el 是跟着手指走的那块（不给就借下面那层的，比如弹窗里的上一步）；on 为假时不算。 */
export function useBack(back: () => void, el?: RefObject<HTMLElement | null>, on = true) {
  const fn = useRef(back)
  fn.current = back
  useEffect(() => on ? push({ back: () => fn.current(), el }) : undefined, [on])
}

/** 手机上的侧栏：关着时右滑把它拉出来（open），开着时它就是最上层。 */
export function useDrawer(open: () => void, isOpen: boolean) {
  const fn = useRef(open)
  fn.current = open
  useEffect(() => {
    if (isOpen) return push({ back: null })
    drawer = () => fn.current()
    return () => { drawer = null }
  }, [isOpen])
}

type Drag = {
  id: number; x0: number; y0: number; dx: number; moving: boolean
  back: () => void; el: HTMLElement | null; mask: HTMLElement | null; width: number
  open: boolean                     // 拉的是侧栏
  trail: [number, number][]         // 最近的 (时间, x)，算松手时的速度
}
let drag: Drag | null = null
let settling = false

// 起手的地方能往左滚回去（横向滚动区已经滚过）：让它滚
function scrolledAway(el: Element | null) {
  for (; el && el !== document.body; el = el.parentElement)
    if (el.scrollLeft > 0 && el.scrollWidth > el.clientWidth) return true
  return false
}

function start(e: TouchEvent) {
  if (drag || settling || e.touches.length !== 1 || !phone?.matches) return
  const t = e.touches[0]
  if (t.clientX > EDGE) return
  const target = e.target instanceof Element ? e.target : null
  if (target?.closest('.pc-view.live, [data-no-swipe]') || scrolledAway(target)) return
  const top = layers[layers.length - 1]
  if (top ? !top.back : !drawer) return
  let el: HTMLElement | null = null
  for (let i = layers.length - 1; i >= 0 && layers[i].back && !el; i--) el = layers[i].el?.current || null
  const open = !top
  if (open) el = document.querySelector<HTMLElement>('.app > .sidebar')
  drag = {
    id: t.identifier, x0: t.clientX, y0: t.clientY, dx: 0, moving: false, open,
    back: top ? top.back! : drawer!, el, mask: open ? document.querySelector<HTMLElement>('.app > .side-mask') : null,
    width: 0, trail: [],
  }
}

function paint(d: Drag) {
  if (d.open) {
    if (d.el) d.el.style.transform = `translateX(${Math.min(0, d.dx - d.width)}px)`
    if (d.mask) d.mask.style.opacity = String(Math.min(1, d.dx / d.width))
  } else if (d.el) d.el.style.transform = `translateX(${d.dx}px)`
}

function move(e: TouchEvent) {
  const d = drag
  const t = d && Array.from(e.changedTouches).find(x => x.identifier === d.id)
  if (!d || !t) return
  const dx = t.clientX - d.x0, dy = t.clientY - d.y0
  if (!d.moving) {
    if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) {
      if (Math.abs(dx) > Math.abs(dy) && e.cancelable) e.preventDefault()   // 偏横着：别让页面先滚起来
      return
    }
    // 竖着滑、往左滑、页面已经在滚了、又按上了一根手指：不是返回
    if (dx < SLOP || Math.abs(dx) < Math.abs(dy) * 1.5 || !e.cancelable || e.touches.length > 1) { drag = null; return }
    d.moving = true
    d.width = d.el?.offsetWidth || window.innerWidth
    if (d.el) d.el.style.transition = 'none'
    if (d.mask) { d.mask.style.transition = 'none'; d.mask.classList.add('dragging') }
    document.documentElement.classList.add('swiping')
  }
  e.preventDefault()
  d.dx = Math.max(0, dx)
  d.trail = [...d.trail.filter(([ms]) => e.timeStamp - ms < 100), [e.timeStamp, t.clientX]]
  paint(d)
}

function end(e: TouchEvent) {
  const d = drag
  if (!d || !Array.from(e.changedTouches).some(x => x.identifier === d.id)) return
  drag = null
  if (!d.moving) return
  const [first, last] = [d.trail[0], d.trail[d.trail.length - 1]]
  const speed = first && last && last[0] > first[0] ? (last[1] - first[1]) / (last[0] - first[0]) : 0   // px/ms，往右为正
  settle(d, e.type === 'touchend' && (d.dx > d.width / 3 || (speed > 0.5 && d.dx > 30)))
}

function settle(d: Drag, done: boolean) {
  settling = true
  if (d.el) {
    d.el.style.transition = `transform ${MS}ms ease-out`
    d.el.style.transform = d.open ? `translateX(${done ? 0 : -d.width}px)` : `translateX(${done ? window.innerWidth : 0}px)`
  }
  if (d.mask) { d.mask.style.transition = `opacity ${MS}ms`; d.mask.style.opacity = done ? '1' : '0' }
  setTimeout(() => {
    try {
      if (done) flushSync(d.back)   // 先让界面换到上一层，再撤掉位移，免得原来那层闪回来
    } finally {
      for (const el of [d.el, d.mask]) if (el) { el.style.transition = ''; el.style.transform = ''; el.style.opacity = '' }
      d.mask?.classList.remove('dragging')
      document.documentElement.classList.remove('swiping')
      settling = false
    }
  }, d.el ? MS : 0)
}

if (typeof document !== 'undefined') {
  document.addEventListener('touchstart', start, { passive: true })
  document.addEventListener('touchmove', move, { passive: false })
  document.addEventListener('touchend', end)
  document.addEventListener('touchcancel', end)
}

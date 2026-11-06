// 桌面 App（Tauri 外壳）在页面加载前放进来的 window.osaDesktop。普通浏览器里没有，下面的东西都不起作用。
// 外部链接、新窗口、下载由外壳自己处理，页面不用管。点了系统通知，外壳发一个 osa-open-chat 事件（detail 是对话 id）。
export type Bridge = {
  notify(title: string, body: string, chatId?: number): void
  setBadge(count: number): void   // 菜单栏和程序坞上显示的待确认数
  speech?: boolean                // 外壳里能用语音识别时为 true；否则不显示麦克风
}

export const desktop: Bridge | undefined = typeof window !== 'undefined' ? (window as any).osaDesktop : undefined

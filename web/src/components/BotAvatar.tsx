import type { Avatar } from '../types'
import { t } from '../i18n'

// 形状都画在 100×100 的画布里；眼睛统一画在中间偏上
const SHAPES: Record<string, string> = {
  square: 'M22 8h56a14 14 0 0 1 14 14v56a14 14 0 0 1-14 14H22A14 14 0 0 1 8 78V22A14 14 0 0 1 22 8z',
  circle: 'M50 6a44 44 0 1 1 0 88a44 44 0 1 1 0-88z',
  sparkle: 'M50 2C55 32 68 45 98 50C68 55 55 68 50 98C45 68 32 55 2 50C32 45 45 32 50 2z',
  star: 'M50 4l13 29 31 3-23 21 7 31-28-16-28 16 7-31L6 36l31-3z',
  heart: 'M50 92C20 70 4 54 4 34A22 22 0 0 1 50 22a22 22 0 0 1 46 12c0 20-16 36-46 58z',
  house: 'M50 6l44 36v44a8 8 0 0 1-8 8H14a8 8 0 0 1-8-8V42z',
  clover: 'M50 50m-22 0a22 22 0 1 1 44 0a22 22 0 1 1-44 0M50 6a20 20 0 0 1 0 40a20 20 0 0 1 0-40M50 54a20 20 0 0 1 0 40a20 20 0 0 1 0-40M6 50a20 20 0 0 1 40 0a20 20 0 0 1-40 0M54 50a20 20 0 0 1 40 0a20 20 0 0 1-40 0',
  flower: 'M50 4c10 0 16 10 14 20 8-6 20-4 24 6s-2 18-12 20c10 2 16 10 12 20s-16 12-24 6c2 10-4 20-14 20s-16-10-14-20c-8 6-20 4-24-6s2-18 12-20C14 48 8 40 12 30s16-12 24-6C34 14 40 4 50 4z',
}
const EYES: Record<string, number> = { sparkle: 46, star: 50, heart: 42, house: 56 }

export default function BotAvatar({ avatar, size = 32, busy = false }: { avatar: Avatar; size?: number; busy?: boolean }) {
  const d = SHAPES[avatar.shape] || SHAPES.circle
  const y = EYES[avatar.shape] ?? 46
  return (
    <svg className={'bot-avatar' + (busy ? ' busy' : '')} width={size} height={size} viewBox="0 0 100 100" aria-hidden>
      <path d={d} fill={avatar.color} fillRule="nonzero" />
      <g className="eyes" fill="#111">
        <ellipse cx="38" cy={y} rx="5.5" ry="8" />
        <ellipse cx="62" cy={y} rx="5.5" ry="8" />
      </g>
    </svg>
  )
}

export const SHAPE_NAMES: Record<string, string> = {
  square: t('方块'), circle: t('圆'), sparkle: t('闪光'), star: t('星星'), heart: t('爱心'), house: t('房子'), clover: t('四叶草'), flower: t('花'),
}

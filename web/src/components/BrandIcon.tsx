import { BRANDS } from '../brandIcons'
import { GLYPHS } from '../appIcons'
import { Icon } from '../icons'

// 品牌图标：品牌色底 + 白色（或深色）图形。应用、模型服务、模型、聊天软件都用它

// 相对亮度：太亮的底用深色图形；接近黑色的品牌（OpenAI、Notion、GitHub）改成白底黑图
export const lum = (hex: string) => {
  const n = parseInt(hex.replace('#', ''), 16)
  const c = [n >> 16, (n >> 8) & 255, n & 255].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4 })
  return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
}

type Props = {
  brand?: string                 // brandIcons.ts 里的名字
  color?: string; d?: string     // 应用清单里的品牌色和 24×24 的 path
  glyph?: string                 // 线条图标（appIcons.ts 的 GLYPHS）
  icon?: string                  // 界面图标（icons.tsx），比如自定义服务的 server
  letter?: string                // 都没有时显示首字
  size?: number; title?: string; className?: string
}

export function BrandIcon({ brand, color, d, glyph, icon, letter, size = 32, title, className }: Props) {
  const b = brand ? BRANDS[brand] : undefined
  const hex = b?.c || color || '#5b5b63'
  const dark = lum(hex) < .02
  const bg = dark ? '#fff' : hex
  const fg = dark ? hex : lum(hex) > .55 ? '#141414' : '#fff'
  const inner = Math.round(size * (size <= 20 ? .66 : .56))
  const line = glyph && GLYPHS[glyph]
  return (
    <span className={'brand-icon' + (className ? ' ' + className : '')} title={title}
      style={{ width: size, height: size, background: bg, color: fg, borderRadius: Math.round(size * .26), fontSize: size * .44 }}>
      {b ? <svg viewBox={b.v || '0 0 24 24'} width={inner} height={inner} aria-hidden dangerouslySetInnerHTML={{ __html: b.s }} />
        : d ? <svg viewBox="0 0 24 24" width={inner} height={inner} aria-hidden><path d={d} fill="currentColor" /></svg>
        : line ? <svg viewBox="0 0 24 24" width={inner} height={inner} fill="none" stroke="currentColor" strokeWidth={2}
            strokeLinecap="round" strokeLinejoin="round" aria-hidden dangerouslySetInnerHTML={{ __html: line }} />
        : icon ? <Icon name={icon} size={inner} />
        : (letter || '?').slice(0, 1).toUpperCase()}
    </span>
  )
}

// ---------- 模型服务是哪个品牌：先看服务商，自定义的按接口地址认 ----------
const PRESET_BRAND: Record<string, string> = {
  deepseek: 'deepseek', openai: 'openai', openrouter: 'openrouter', siliconflow: 'siliconcloud',
  dashscope: 'bailian', zhipu: 'zhipu', moonshot: 'kimi', ollama: 'ollama',
}
const URL_BRAND: [RegExp, string][] = [
  [/deepseek\.com/, 'deepseek'], [/openai\.azure\.com|azure\.com/, 'azure'], [/api\.openai\.com/, 'openai'], [/openrouter\.ai/, 'openrouter'],
  [/siliconflow/, 'siliconcloud'], [/dashscope|bailian/, 'bailian'], [/bigmodel\.cn|api\.z\.ai/, 'zhipu'], [/moonshot/, 'kimi'],
  [/anthropic\.com/, 'anthropic'], [/generativelanguage\.googleapis|aiplatform\.googleapis/, 'gemini'], [/api\.x\.ai/, 'xai'],
  [/mistral\.ai/, 'mistral'], [/groq\.com/, 'groq'], [/together\.(ai|xyz)/, 'together'], [/fireworks\.ai/, 'fireworks'],
  [/volces\.com|volcengine/, 'volcengine'], [/hunyuan|tencentcloudapi/, 'hunyuan'], [/baidubce|qianfan/, 'baiducloud'],
  [/minimax/, 'minimax'], [/lingyiwanwu|01\.ai/, 'yi'], [/baichuan/, 'baichuan'], [/stepfun/, 'stepfun'],
  [/nvidia\.com/, 'nvidia'], [/perplexity\.ai/, 'perplexity'], [/huggingface\.co/, 'huggingface'], [/modelscope/, 'modelscope'],
  [/jina\.ai/, 'jina'], [/cohere/, 'cohere'], [/:11434\b|ollama/, 'ollama'], [/:1234\b/, 'lmstudio'],
]
export function providerBrand(preset?: string, url = '') {
  if (preset && PRESET_BRAND[preset]) return PRESET_BRAND[preset]
  const u = url.toLowerCase()
  return URL_BRAND.find(([re]) => re.test(u))?.[1] || ''
}

// ---------- 模型是哪家的：按名字认，OpenRouter 这类带前缀的（openai/gpt-…）也认得 ----------
const MODEL_BRAND: [RegExp, string][] = [
  [/deepseek/, 'deepseek'],
  [/claude|anthropic\//, 'claude'],
  [/gemma/, 'gemma'],
  [/gemini|^google\//, 'gemini'],
  [/qwen|qwq|qvq|tongyi|^text-embedding-v\d|(^|\/)gte-/, 'qwen'],
  [/glm|^cogview|^cogvideo|^embedding-[23]$|^(zai-org|z-ai|thudm)\//, 'chatglm'],
  [/kimi|moonshot/, 'kimi'],
  [/llama/, 'meta'],
  [/mistral|mixtral|codestral|pixtral|ministral|magistral|devstral|voxtral/, 'mistral'],
  [/grok|^x-ai\//, 'grok'],
  [/doubao|(^|\/)seed-|bytedance/, 'doubao'],
  [/hunyuan|^tencent\//, 'hunyuan'],
  [/ernie|^baidu\//, 'wenxin'],
  [/minimax|abab/, 'minimax'],
  [/(^|\/)yi-/, 'yi'],
  [/baichuan/, 'baichuan'],
  [/(^|\/)step-/, 'stepfun'],
  [/internlm|intern-s/, 'internlm'],
  [/(^|\/)phi-|^microsoft\//, 'microsoft'],
  [/(^|\/)(command|c4ai|embed-(english|multilingual)|rerank-)|cohere/, 'cohere'],
  [/bge-|baai\//, 'baai'],
  [/jina/, 'jina'],
  [/(^|\/)(ling|ring)-|inclusionai/, 'ling'],
  [/nemotron|^nvidia\//, 'nvidia'],
  [/(^|\/)(gpt|o\d|chatgpt|text-embedding-(3|ada)|dall-e|whisper|tts-1|codex|omni-moderation)|^openai\//, 'openai'],
]
export function modelBrand(model: string) {
  const m = (model || '').toLowerCase()
  return MODEL_BRAND.find(([re]) => re.test(m))?.[1] || ''
}

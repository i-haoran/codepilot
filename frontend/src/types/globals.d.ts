/**
 * 补充第三方库的类型声明。
 *
 * `react-syntax-highlighter` 不自带类型，官方类型包 @types/react-syntax-highlighter
 * 也不在依赖里，导致 `tsc` 在每个 import 处报 TS7016（implicitly has an 'any' type），
 * 进而让 `npm run build`（tsc && vite build）失败。
 *
 * 这里给出最小可用的类型声明：只描述本项目实际用到的部分，
 * 避免为了一个高亮组件引入整套类型依赖。
 */

declare module 'react-syntax-highlighter' {
  import type { ComponentType, ReactNode } from 'react'

  export interface SyntaxHighlighterProps {
    language?: string
    style?: Record<string, unknown>
    PreTag?: string | ComponentType<{ children?: ReactNode }>
    children?: ReactNode
    [key: string]: unknown
  }

  export const Prism: ComponentType<SyntaxHighlighterProps>
  export const Light: ComponentType<SyntaxHighlighterProps>
  export default Prism
}

declare module 'react-syntax-highlighter/dist/esm/styles/prism' {
  const style: Record<string, unknown>
  export const tomorrow: Record<string, unknown>
  export default style
}

declare module 'react-syntax-highlighter/dist/cjs/styles/prism' {
  const style: Record<string, unknown>
  export const tomorrow: Record<string, unknown>
  export default style
}

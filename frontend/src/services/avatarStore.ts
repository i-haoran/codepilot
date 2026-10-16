import { useEffect, useState } from 'react'
import { profileApi } from './api'

/**
 * 头像共享状态。
 *
 * 为什么需要它：头像在「个人中心」上传，但要在「对话页」的消息气泡里显示，
 * 两者之间隔了好几层路由组件。用 prop 逐层透传会把中间组件污染得很脏，
 * 而且上传后需要刷新页面才能同步。
 *
 * 这里用一个极小的订阅式 store：
 * · `useAvatar()` 让任意组件订阅头像变化
 * · `setAvatar()` 在个人中心上传成功后调用，所有订阅处立即重渲染
 */

let avatar = ''
let loaded = false
const listeners = new Set<(value: string) => void>()

function emit() {
  listeners.forEach((fn) => fn(avatar))
}

export function getAvatar(): string {
  return avatar
}

export function setAvatar(next: string) {
  if (avatar === next) return
  avatar = next || ''
  emit()
}

/** 只在首次调用时真正请求后端，避免每个组件各拉一次。 */
export async function ensureAvatarLoaded(force = false): Promise<string> {
  if (loaded && !force) return avatar
  try {
    const res = await profileApi.get()
    loaded = true
    setAvatar(res.data.avatar || '')
  } catch {
    // 未登录或接口异常时保持空值，由调用方降级为默认图标
  }
  return avatar
}

/** 退出登录时清空，避免下一个账号看到上一个账号的头像。 */
export function resetAvatar() {
  loaded = false
  setAvatar('')
}

export function useAvatar(): string {
  const [value, setValue] = useState<string>(avatar)

  useEffect(() => {
    listeners.add(setValue)
    // 挂载时若尚未加载过，主动拉一次
    if (!loaded) void ensureAvatarLoaded()
    return () => {
      listeners.delete(setValue)
    }
  }, [])

  return value
}

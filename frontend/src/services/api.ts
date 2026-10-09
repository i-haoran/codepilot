import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      window.location.reload()
    }
    return Promise.reject(err)
  }
)

export interface LoginRequest {
  username: string
  password: string
}

export interface TokenResponse {
  token: string
  user_id: string
  username: string
}

export interface ChatRequest {
  message: string
  conversation_id?: string
  stream?: boolean
}

export interface ChatResponse {
  run_id: string
  response?: string
  conversation_id: string
  status: 'pending' | 'completed' | 'failed'
}

export interface ConversationItem {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export interface HistoryItem {
  id: string
  message: string
  response: string
  created_at: string
}

export interface DocumentItem {
  id: string
  filename: string
  chunk_count: number
  created_at: string
}

export interface ProfileData {
  user_id: string
  username: string
  avatar: string
}

export const authApi = {
  register: (req: LoginRequest) => api.post<TokenResponse>('/auth/register', req),
  login: (req: LoginRequest) => api.post<TokenResponse>('/auth/login', req),
}

export const profileApi = {
  get: () => api.get<ProfileData>('/profile'),
  update: (data: { username?: string }) => api.put('/profile', data),
  uploadAvatar: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post<{ avatar: string }>('/upload-avatar', form)
  },
}

export const conversationApi = {
  list: () => api.get<ConversationItem[]>('/conversations'),
  create: () => api.post<ConversationItem>('/conversations'),
  delete: (id: string) => api.delete(`/conversations/${id}`),
  history: (id: string) => api.get<HistoryItem[]>(`/conversations/${id}/history`),
}

export interface ToolCallItem {
  name: string
  arguments: string
}

export type ChatStreamEvent =
  | { type: 'content'; data: string }
  | { type: 'conv_id'; data: string }
  | { type: 'trace'; data: string[]; toolCalls: ToolCallItem[] }
  | { type: 'error'; data: string }

export const chatStreamApi = {
  /**
   * 以 SSE 方式接收流式回答。
   *
   * 这里用 fetch + ReadableStream 手动解析，而不是 EventSource：
   * EventSource 只支持 GET 且无法自定义请求头，而我们需要 POST 提交消息
   * 并携带 Authorization: Bearer <JWT>。
   */
  send: async function* (req: ChatRequest): AsyncGenerator<ChatStreamEvent> {
    const token = localStorage.getItem('token')
    const response = await fetch('/api/v1/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(req),
    })

    if (!response.ok || !response.body) {
      yield { type: 'error', data: `请求失败：HTTP ${response.status}` }
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      // SSE 以空行分帧；这里按行切分并保留最后一段不完整数据
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (!raw) continue

        try {
          const parsed = JSON.parse(raw)
          if (parsed.type === 'token') {
            yield { type: 'content', data: parsed.content }
          } else if (parsed.type === 'conv_id') {
            yield { type: 'conv_id', data: parsed.content }
          } else if (parsed.type === 'trace') {
            yield {
              type: 'trace',
              data: Array.isArray(parsed.content) ? parsed.content : [String(parsed.content)],
              toolCalls: parsed.tool_calls ?? [],
            }
          } else if (parsed.type === 'error') {
            yield { type: 'error', data: parsed.content }
          } else if (parsed.type === 'done') {
            return
          }
        } catch {
          // 忽略无法解析的帧，避免单帧异常中断整个流
        }
      }
    }
  },
}

export const documentApi = {
  list: () => api.get<DocumentItem[]>('/documents'),
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post<DocumentItem>('/documents', form)
  },
  delete: (id: string) => api.delete(`/documents/${id}`),
}

export default api

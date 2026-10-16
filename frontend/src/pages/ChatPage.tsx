import { useState, useEffect, useRef } from 'react'
import { chatStreamApi, conversationApi, ConversationItem, AnswerSource } from '../services/api'
import ChatMessage from '../components/ChatMessage'
import ChatInput from '../components/ChatInput'
import './ChatPage.css'

interface ToolCallItem {
  name: string
  arguments: string
}

interface Message {
  id: string
  content: string
  role: 'user' | 'assistant'
  timestamp: Date
  agentTrace?: string[]
  toolCalls?: ToolCallItem[]
  /** 答案来源，用于在气泡上标注「是否基于知识库」 */
  answerSource?: AnswerSource
  sourceLabel?: string
}

function ChatPage() {
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [currentConvId, setCurrentConvId] = useState<string>('')
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [convSidebarOpen, setConvSidebarOpen] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const streamIdRef = useRef(0)

  // 无上传头像时用于显示用户名首字母
  const username = (() => {
    try {
      return JSON.parse(localStorage.getItem('user') || '{}').username || ''
    } catch {
      return ''
    }
  })()

  useEffect(() => {
    loadConversations()
  }, [])

  useEffect(() => {
    if (currentConvId) {
      loadHistory(currentConvId)
    } else {
      setMessages([])
    }
  }, [currentConvId])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const loadConversations = async () => {
    try {
      const res = await conversationApi.list()
      setConversations(res.data)
    } catch {}
  }

  const loadHistory = async (convId: string) => {
    try {
      const res = await conversationApi.history(convId)
      setMessages(res.data.map(r => [
        { id: r.id + '-q', content: r.message, role: 'user' as const, timestamp: new Date(r.created_at) },
        { id: r.id + '-a', content: r.response, role: 'assistant' as const, timestamp: new Date(r.created_at) },
      ]).flat())
    } catch {
      setMessages([])
    }
  }

  const handleNewChat = async () => {
    try {
      const res = await conversationApi.create()
      setConversations(prev => [res.data, ...prev])
      setCurrentConvId(res.data.id)
      setMessages([])
    } catch {}
  }

  const handleDeleteConv = async (id: string) => {
    if (!confirm('确认删除该会话？')) return
    try {
      await conversationApi.delete(id)
      setConversations(prev => prev.filter(c => c.id !== id))
      if (currentConvId === id) {
        setCurrentConvId('')
        setMessages([])
      }
    } catch {}
  }

  const handleSendMessage = async (text: string) => {
    if (!text || isLoading) return

    const userMsg: Message = {
      id: Date.now().toString(),
      content: text,
      role: 'user',
      timestamp: new Date()
    }
    setMessages(prev => [...prev, userMsg])
    setIsLoading(true)

    const currentStreamId = ++streamIdRef.current
    let convId = currentConvId
    let agentTrace: string[] = []
    let toolCalls: ToolCallItem[] = []
    let answerSource: AnswerSource | undefined
    let sourceLabel: string | undefined

    try {
      let assistantContent = ''
      let gotFirstChunk = false

      for await (const chunk of chatStreamApi.send({
        message: text,
        conversation_id: convId || undefined,
        stream: true
      })) {
        if (currentStreamId !== streamIdRef.current) return

        if (chunk.type === 'conv_id') {
          convId = chunk.data
          setCurrentConvId(chunk.data)
          loadConversations()
        } else if (chunk.type === 'trace') {
          agentTrace = chunk.data
          toolCalls = chunk.toolCalls
          answerSource = chunk.answerSource
          sourceLabel = chunk.sourceLabel
        } else if (chunk.type === 'error') {
          setMessages(prev => [...prev, {
            id: Date.now().toString(),
            content: `错误：${chunk.data}`,
            role: 'assistant',
            timestamp: new Date()
          }])
          break
        } else {
          assistantContent += chunk.data
          if (!gotFirstChunk) {
            gotFirstChunk = true
            setMessages(prev => [...prev, {
              id: 'streaming-' + Date.now(),
              content: assistantContent,
              role: 'assistant',
              timestamp: new Date(),
              agentTrace: agentTrace.length > 0 ? agentTrace : undefined,
              toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
              answerSource,
              sourceLabel,
            }])
          } else {
            setMessages(prev => {
              const updated = [...prev]
              for (let i = updated.length - 1; i >= 0; i--) {
                if (updated[i].role === 'assistant' && updated[i].id.startsWith('streaming')) {
                  updated[i] = {
                    ...updated[i],
                    content: assistantContent,
                    agentTrace: agentTrace.length > 0 ? agentTrace : updated[i].agentTrace,
                    toolCalls: toolCalls.length > 0 ? toolCalls : updated[i].toolCalls,
                    answerSource: answerSource ?? updated[i].answerSource,
                    sourceLabel: sourceLabel ?? updated[i].sourceLabel,
                  }
                  break
                }
              }
              return updated
            })
          }
        }
      }

      setMessages(prev => {
        const updated = [...prev]
        for (let i = updated.length - 1; i >= 0; i--) {
          if (updated[i].role === 'assistant' && updated[i].id.startsWith('streaming')) {
            updated[i] = { ...updated[i], id: Date.now().toString() }
            break
          }
        }
        return updated
      })
    } catch {
      setMessages(prev => [...prev, {
        id: Date.now().toString(),
        content: '抱歉，发生了错误，请稍后重试。',
        role: 'assistant',
        timestamp: new Date()
      }])
    } finally {
      if (currentStreamId === streamIdRef.current) {
        setIsLoading(false)
      }
    }
  }

  return (
    <div className="chat-layout">
      <div className={`conv-overlay ${convSidebarOpen ? 'open' : ''}`} onClick={() => setConvSidebarOpen(false)} />
      <div className={`conv-sidebar ${convSidebarOpen ? 'open' : ''}`}>
        <div className="conv-header">
          <h3>会话列表</h3>
          <button className="btn-new-conv" onClick={handleNewChat}>+ 新建</button>
        </div>
        <div className="conv-list">
          {conversations.map(conv => (
            <div
              key={conv.id}
              className={`conv-item ${currentConvId === conv.id ? 'active' : ''}`}
              onClick={() => { setCurrentConvId(conv.id); setConvSidebarOpen(false) }}
            >
              <div className="conv-title">{conv.title}</div>
              <div className="conv-actions">
                <button className="conv-action-btn delete" title="删除" onClick={(e) => { e.stopPropagation(); handleDeleteConv(conv.id) }}>×</button>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="chat-main">
        <div className="mobile-conv-toggle" onClick={() => setConvSidebarOpen(true)}>
          <span className="toggle-icon">☰</span>
          <span>会话列表</span>
        </div>
        <div className="chat-messages">
          {messages.length === 0 && (
            <div className="welcome-section">
              <div className="welcome-icon">🤖</div>
              <h2>你好！我是CodePilot</h2>
              <p>我是你的研发知识助手，可以帮你查询技术文档、API规范、架构设计等知识。</p>
            </div>
          )}

          {messages.map(msg => (
            <ChatMessage key={msg.id} message={msg} username={username} />
          ))}

          {isLoading && messages[messages.length - 1]?.role !== 'assistant' && (
            <div className="chat-msg assistant">
              <div className="msg-avatar">🤖</div>
              <div className="msg-bubble">
                <div className="typing-indicator">
                  <span></span><span></span><span></span>
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        <ChatInput onSend={handleSendMessage} isLoading={isLoading} />
      </div>
    </div>
  )
}

export default ChatPage

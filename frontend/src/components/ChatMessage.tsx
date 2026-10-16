import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { tomorrow } from 'react-syntax-highlighter/dist/esm/styles/prism'
import type { ReactNode } from 'react'
import type { AnswerSource } from '../services/api'
import { useAvatar } from '../services/avatarStore'
import './ChatMessage.css'

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
  answerSource?: AnswerSource
  sourceLabel?: string
}

interface ChatMessageProps {
  message: Message
  /** 用户名，用于没有头像时显示首字母 */
  username?: string
}

/** 各来源对应的徽标样式与说明 */
const SOURCE_META: Record<string, { cls: string; icon: string; hint: string }> = {
  knowledge_base: { cls: 'src-kb', icon: '📚', hint: '答案基于你上传的知识库文档' },
  tools: { cls: 'src-tool', icon: '🛠', hint: '答案基于工具查询到的实时数据' },
  not_retrieved: { cls: 'src-warn', icon: '⚠️', hint: '检索未命中相关片段，以下内容来自模型通用知识，请自行核实' },
  no_retrieval: { cls: 'src-general', icon: '💬', hint: '通用对话，未检索知识库' },
  refused: { cls: 'src-refused', icon: '🚫', hint: '未能基于现有资料给出可靠回答' },
}

function ChatMessage({ message, username }: ChatMessageProps) {
  const isUser = message.role === 'user'
  // 订阅头像：在个人中心更换后，对话页无需刷新即可同步
  const avatar = useAvatar()

  const renderAvatar = () => {
    if (!isUser) return '🤖'
    // 有上传头像就显示图片，否则回退到用户名首字母 / 默认图标
    if (avatar) {
      return <img className="message-avatar-img" src={avatar} alt="我" />
    }
    return username?.[0]?.toUpperCase() || '👤'
  }

  const sourceKey = message.answerSource || ''
  const meta = SOURCE_META[sourceKey]

  return (
    <div className={`chat-message ${isUser ? 'user' : 'assistant'}`}>
      <div className="message-avatar">
        {renderAvatar()}
      </div>
      <div className="message-content">
        <div className="message-header">
          <span className="message-role">{isUser ? '你' : 'CodePilot'}</span>
          {/* 来源徽标：让用户始终知道答案是否基于知识库 */}
          {!isUser && meta && (
            <span className={`source-badge ${meta.cls}`} title={meta.hint}>
              {meta.icon} {message.sourceLabel || meta.hint}
            </span>
          )}
          <span className="message-time">
            {message.timestamp.toLocaleTimeString()}
          </span>
        </div>
        <div className="message-body">
          <ReactMarkdown
            components={{
              // react-markdown 9.x 不再向 components 传 `inline`，改用 className
              // 里是否带 language-xxx 来区分「代码块」与「行内代码」
              code({ className, children }: {
                className?: string
                children?: ReactNode
              }) {
                const match = /language-(\w+)/.exec(className || '')
                return match ? (
                  <SyntaxHighlighter
                    style={tomorrow}
                    language={match[1]}
                    PreTag="div"
                  >
                    {String(children).replace(/\n$/, '')}
                  </SyntaxHighlighter>
                ) : (
                  <code className={className}>{children}</code>
                )
              },
              p({ children }) {
                return <p style={{ margin: '8px 0' }}>{children}</p>
              },
              ul({ children }) {
                return <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>{children}</ul>
              },
              ol({ children }) {
                return <ol style={{ margin: '8px 0', paddingLeft: '20px' }}>{children}</ol>
              },
              li({ children }) {
                return <li style={{ margin: '4px 0' }}>{children}</li>
              },
              h1({ children }) {
                return <h1 style={{ margin: '16px 0 8px', fontSize: '20px' }}>{children}</h1>
              },
              h2({ children }) {
                return <h2 style={{ margin: '14px 0 8px', fontSize: '18px' }}>{children}</h2>
              },
              h3({ children }) {
                return <h3 style={{ margin: '12px 0 8px', fontSize: '16px' }}>{children}</h3>
              },
              strong({ children }) {
                return <strong style={{ color: '#1f2937' }}>{children}</strong>
              }
            }}
          >
            {message.content}
          </ReactMarkdown>
        </div>
        {message.agentTrace && message.agentTrace.length > 0 && (
          <div className="message-trace">
            <details>
              <summary>🔍 查看执行轨迹</summary>
              <div className="trace-steps">
                {message.agentTrace.map((trace, index) => (
                  <div key={index} className="trace-step">
                    <span className="step-num">{index + 1}</span>
                    <span className="step-text">{trace}</span>
                  </div>
                ))}
                {message.toolCalls && message.toolCalls.length > 0 && (
                  <div className="trace-step">
                    <span className="step-num">🛠</span>
                    <span className="step-text">
                      工具调用参数：
                      {message.toolCalls.map((call, index) => (
                        <code key={index} style={{ marginLeft: 6 }}>
                          {call.name}({call.arguments})
                        </code>
                      ))}
                    </span>
                  </div>
                )}
              </div>
            </details>
          </div>
        )}
      </div>
    </div>
  )
}

export default ChatMessage

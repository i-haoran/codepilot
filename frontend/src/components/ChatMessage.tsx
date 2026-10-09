import React from 'react'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { tomorrow } from 'react-syntax-highlighter/dist/esm/styles/prism'
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
}

interface ChatMessageProps {
  message: Message
}

function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`chat-message ${isUser ? 'user' : 'assistant'}`}>
      <div className="message-avatar">
        {isUser ? '👤' : '🤖'}
      </div>
      <div className="message-content">
        <div className="message-header">
          <span className="message-role">{isUser ? '你' : 'CodePilot'}</span>
          <span className="message-time">
            {message.timestamp.toLocaleTimeString()}
          </span>
        </div>
        <div className="message-body">
          <ReactMarkdown
            components={{
              code({ node, inline, className, children, ...props }) {
                const match = /language-(\w+)/.exec(className || '')
                return !inline && match ? (
                  <SyntaxHighlighter
                    style={tomorrow}
                    language={match[1]}
                    PreTag="div"
                    {...props}
                  >
                    {String(children).replace(/\n$/, '')}
                  </SyntaxHighlighter>
                ) : (
                  <code className={className} {...props}>
                    {children}
                  </code>
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

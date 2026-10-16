import React, { useState, useEffect } from 'react'
import { conversationApi, HistoryItem } from '../services/api'
import './HistoryPage.css'

function HistoryPage() {
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [selectedItem, setSelectedItem] = useState<HistoryItem | null>(null)
  const [searchQuery, setSearchQuery] = useState('')

  useEffect(() => {
    loadAllHistory()
  }, [])

  const loadAllHistory = async () => {
    try {
      const convs = await conversationApi.list()
      const allItems: HistoryItem[] = []
      for (const conv of convs.data) {
        const res = await conversationApi.history(conv.id)
        allItems.push(...res.data)
      }
      allItems.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
      setHistory(allItems)
    } catch {
      setHistory([])
    }
  }

  const filteredHistory = history.filter(item =>
    item.message.includes(searchQuery) || item.response.includes(searchQuery)
  )

  return (
    <div className="history-page">
      <div className="page-header">
        <div>
          <h1>📋 历史记录</h1>
          <p>查看之前的问答记录</p>
        </div>
      </div>

      <div className="history-layout">
        <div className="history-list">
          <div className="search-box">
            <input
              type="text"
              placeholder="搜索历史记录..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>

          {filteredHistory.length === 0 ? (
            <div className="history-empty">暂无记录</div>
          ) : (
            filteredHistory.map(item => (
              <div
                key={item.id}
                className={`history-item ${selectedItem?.id === item.id ? 'active' : ''}`}
                onClick={() => setSelectedItem(item)}
              >
                <div className="history-question">{item.message}</div>
                <div className="history-meta">
                  <span className="history-time">{new Date(item.created_at).toLocaleString()}</span>
                </div>
              </div>
            ))
          )}
        </div>

        <div className="history-detail">
          {selectedItem ? (
            <>
              <div className="detail-header">
                <h3>问题</h3>
                <p className="detail-question">{selectedItem.message}</p>
              </div>

              <div className="detail-section">
                <h3>回答</h3>
                <div className="detail-answer">
                  {selectedItem.response.split('\n').map((line, i) => (
                    <React.Fragment key={i}>
                      {line}
                      {i < selectedItem.response.split('\n').length - 1 && <br />}
                    </React.Fragment>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div className="empty-detail">
              <div className="empty-icon">📝</div>
              <p>选择一条记录查看详情</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default HistoryPage

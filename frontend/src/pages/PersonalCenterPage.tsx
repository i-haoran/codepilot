import { useState, useEffect, useRef } from 'react'
import { profileApi, documentApi, conversationApi, ProfileData, DocumentItem, ConversationItem, HistoryItem } from '../services/api'
import { setAvatar as publishAvatar } from '../services/avatarStore'
import './PersonalCenterPage.css'

function stripMarkdown(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/`{3}[\s\S]*?`{3}/g, (m) => m.replace(/`{3}\w*\n?/g, '').replace(/`{3}/g, ''))
    .replace(/`(.+?)`/g, '$1')
    .replace(/^\s*[-*+]\s+/gm, '• ')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/---+/g, '')
    .trim()
}

function PersonalCenterPage() {
  const [profile, setProfile] = useState<ProfileData>({ user_id: '', username: '', avatar: '' })
  const [documents, setDocuments] = useState<DocumentItem[]>([])
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [selectedConv, setSelectedConv] = useState<ConversationItem | null>(null)
  const [convHistory, setConvHistory] = useState<HistoryItem[]>([])
  const [activeTab, setActiveTab] = useState<'docs' | 'history'>('docs')
  const [showUploadModal, setShowUploadModal] = useState(false)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [editingName, setEditingName] = useState(false)
  const [newName, setNewName] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    loadProfile()
    loadDocuments()
    loadConversations()
  }, [])

  const loadProfile = async () => {
    try {
      const res = await profileApi.get()
      setProfile(res.data)
      setNewName(res.data.username)
      // 同步给全局 store：对话页气泡与侧边栏会立即跟着更新
      publishAvatar(res.data.avatar || '')
    } catch {}
  }

  const loadDocuments = async () => {
    try {
      const res = await documentApi.list()
      setDocuments(res.data)
    } catch {}
  }

  const loadConversations = async () => {
    try {
      const res = await conversationApi.list()
      setConversations(res.data)
    } catch {}
  }

  const handleAvatarUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      const res = await profileApi.uploadAvatar(file)
      setProfile(prev => ({ ...prev, avatar: res.data.avatar }))
      // 关键：同时写入全局 store，否则只有本页知道换过头像，
      // 对话页的气泡仍显示旧图标，且必须刷新页面才能同步
      publishAvatar(res.data.avatar || '')
    } catch {
      alert('头像上传失败')
    } finally {
      // 允许连续选择同一个文件（否则 input 的 value 不变不会触发 change）
      e.target.value = ''
    }
  }

  const handleUpdateName = async () => {
    if (!newName.trim()) return
    try {
      await profileApi.update({ username: newName })
      setProfile(prev => ({ ...prev, username: newName }))
      const user = JSON.parse(localStorage.getItem('user') || '{}')
      user.username = newName
      localStorage.setItem('user', JSON.stringify(user))
      setEditingName(false)
    } catch {
      alert('修改失败')
    }
  }

  const handleUpload = async () => {
    if (!selectedFile) return
    setUploading(true)
    try {
      await documentApi.upload(selectedFile)
      setShowUploadModal(false)
      setSelectedFile(null)
      loadDocuments()
    } catch {
      alert('上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handleDeleteDoc = async (id: string) => {
    if (!confirm('确认删除该文档？')) return
    try {
      await documentApi.delete(id)
      loadDocuments()
    } catch {}
  }

  const handleSelectConv = async (conv: ConversationItem) => {
    setSelectedConv(conv)
    try {
      const res = await conversationApi.history(conv.id)
      setConvHistory(res.data)
    } catch {}
  }

  const handleDeleteConv = async (id: string) => {
    if (!confirm('确认删除该会话？')) return
    try {
      await conversationApi.delete(id)
      setConversations(prev => prev.filter(c => c.id !== id))
      if (selectedConv?.id === id) {
        setSelectedConv(null)
        setConvHistory([])
      }
    } catch {}
  }

  return (
    <div className="profile-page">
      <div className="profile-header">
        <div className="avatar-section" onClick={() => fileInputRef.current?.click()}>
          <div className="avatar-circle">
            {profile.avatar ? (
              <img src={profile.avatar} alt="avatar" />
            ) : (
              <span className="avatar-text">{profile.username?.[0]?.toUpperCase() || '?'}</span>
            )}
          </div>
          <div className="avatar-overlay">更换头像</div>
          <input ref={fileInputRef} type="file" accept="image/*" onChange={handleAvatarUpload} hidden />
        </div>

        <div className="profile-info">
          {editingName ? (
            <div className="name-edit">
              <input value={newName} onChange={e => setNewName(e.target.value)} onKeyDown={e => e.key === 'Enter' && handleUpdateName()} />
              <button onClick={handleUpdateName}>保存</button>
              <button onClick={() => setEditingName(false)}>取消</button>
            </div>
          ) : (
            <h2 onClick={() => setEditingName(true)} className="name-clickable">{profile.username} ✏️</h2>
          )}
        </div>
      </div>

      <div className="profile-tabs">
        <button className={`tab ${activeTab === 'docs' ? 'active' : ''}`} onClick={() => setActiveTab('docs')}>📚 知识库</button>
        <button className={`tab ${activeTab === 'history' ? 'active' : ''}`} onClick={() => setActiveTab('history')}>📋 聊天记录</button>
      </div>

      <div className="profile-content">
        {activeTab === 'docs' && (
          <div className="docs-section">
            <div className="section-header">
              <h3>文档列表</h3>
              <button className="btn-primary-sm" onClick={() => setShowUploadModal(true)}>+ 上传</button>
            </div>
            <div className="doc-stats">
              <span>共 {documents.length} 个文档</span>
              <span>{documents.reduce((s, d) => s + d.chunk_count, 0)} 个分片</span>
            </div>
            {documents.length === 0 ? (
              <div className="empty-hint">暂无文档，上传文件后AI问答会自动参考</div>
            ) : (
              <div className="doc-list">
                {documents.map(doc => (
                  <div key={doc.id} className="doc-item">
                    <span className="doc-icon">📄</span>
                    <span className="doc-name">{doc.filename}</span>
                    <span className="doc-chunks">{doc.chunk_count}片</span>
                    <button className="btn-delete-sm" onClick={() => handleDeleteDoc(doc.id)}>删除</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {activeTab === 'history' && (
          <div className="history-section">
            <div className="history-split">
              <div className="history-conv-list">
                <h3>会话记录</h3>
                {conversations.length === 0 ? (
                  <div className="empty-hint">暂无记录</div>
                ) : (
                  conversations.map(conv => (
                    <div
                      key={conv.id}
                      className={`history-conv-item ${selectedConv?.id === conv.id ? 'active' : ''}`}
                      onClick={() => handleSelectConv(conv)}
                    >
                      <span className="hcv-title">{conv.title}</span>
                      <button className="btn-delete-sm" onClick={(e) => { e.stopPropagation(); handleDeleteConv(conv.id) }}>×</button>
                    </div>
                  ))
                )}
              </div>
              <div className="history-detail">
                {selectedConv ? (
                  <>
                    <h3>{selectedConv.title}</h3>
                    <div className="history-messages">
                      {convHistory.map(h => (
                        <div key={h.id} className="hm-item">
                          <div className="hm-q">{h.message}</div>
                          <div className="hm-a">{stripMarkdown(h.response)}</div>
                          <div className="hm-time">{new Date(h.created_at).toLocaleString()}</div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="empty-hint">选择一个会话查看详情</div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {showUploadModal && (
        <div className="modal-overlay" onClick={() => setShowUploadModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>上传文档</h3>
            <div className="upload-zone" onClick={() => document.getElementById('file-input-modal')?.click()}>
              {selectedFile ? (
                <div><p>📄 {selectedFile.name}</p><p className="hint">{(selectedFile.size / 1024).toFixed(1)} KB</p></div>
              ) : (
                <><div className="upload-icon">📤</div><p>点击选择文件</p><p className="hint">支持 txt、md 等文本格式</p></>
              )}
              <input id="file-input-modal" type="file" accept=".txt,.md,.py,.js,.ts,.json,.csv" onChange={e => setSelectedFile(e.target.files?.[0] || null)} hidden />
            </div>
            <div className="modal-actions">
              <button className="btn-secondary" onClick={() => { setShowUploadModal(false); setSelectedFile(null) }}>取消</button>
              <button className="btn-primary-sm" disabled={!selectedFile || uploading} onClick={handleUpload}>
                {uploading ? '上传中...' : '上传'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default PersonalCenterPage

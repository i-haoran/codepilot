import { useState, useEffect } from 'react'
import { documentApi, DocumentItem } from '../services/api'
import './KnowledgeBasePage.css'

function KnowledgeBasePage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([])
  const [showUploadModal, setShowUploadModal] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)

  useEffect(() => {
    loadDocuments()
  }, [])

  const loadDocuments = async () => {
    try {
      const res = await documentApi.list()
      setDocuments(res.data)
    } catch {
      setDocuments([])
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
    } catch (err) {
      alert('上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (id: string) => {
    if (!confirm('确认删除该文档？')) return
    try {
      await documentApi.delete(id)
      loadDocuments()
    } catch {
      alert('删除失败')
    }
  }

  return (
    <div className="knowledge-page">
      <div className="page-header">
        <div>
          <h1>📚 知识库管理</h1>
          <p>上传文档，AI问答时可选择参考</p>
        </div>
        <button className="btn-primary" onClick={() => setShowUploadModal(true)}>
          + 上传文档
        </button>
      </div>

      <div className="knowledge-content">
        <div className="doc-stats">
          <div className="stat-card">
            <div className="stat-value">{documents.length}</div>
            <div className="stat-label">文档总数</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{documents.reduce((s, d) => s + d.chunk_count, 0)}</div>
            <div className="stat-label">文本分片</div>
          </div>
        </div>

        <div className="doc-table">
          <div className="doc-table-header">
            <span className="col-name">文件名</span>
            <span className="col-chunks">分片数</span>
            <span className="col-time">上传时间</span>
            <span className="col-actions">操作</span>
          </div>
          {documents.length === 0 ? (
            <div className="doc-empty">
              <p>暂无文档，请上传文件</p>
            </div>
          ) : (
            documents.map(doc => (
              <div key={doc.id} className="doc-table-row">
                <span className="col-name">📄 {doc.filename}</span>
                <span className="col-chunks">{doc.chunk_count}</span>
                <span className="col-time">{new Date(doc.created_at).toLocaleString()}</span>
                <span className="col-actions">
                  <button className="btn-danger-sm" onClick={() => handleDelete(doc.id)}>删除</button>
                </span>
              </div>
            ))
          )}
        </div>
      </div>

      {showUploadModal && (
        <div className="modal-overlay" onClick={() => setShowUploadModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>上传文档</h3>
            <div className="upload-zone" onClick={() => document.getElementById('file-input')?.click()}>
              {selectedFile ? (
                <div className="upload-selected">
                  <p>📄 {selectedFile.name}</p>
                  <p className="upload-hint">{(selectedFile.size / 1024).toFixed(1)} KB</p>
                </div>
              ) : (
                <>
                  <div className="upload-icon">📤</div>
                  <p>点击选择文件</p>
                  <p className="upload-hint">支持 txt、md 等文本格式</p>
                </>
              )}
              <input
                id="file-input"
                type="file"
                className="file-input"
                accept=".txt,.md,.py,.js,.ts,.json,.csv"
                onChange={e => setSelectedFile(e.target.files?.[0] || null)}
              />
            </div>
            <div className="modal-actions">
              <button className="btn-secondary" onClick={() => { setShowUploadModal(false); setSelectedFile(null) }}>取消</button>
              <button className="btn-primary" disabled={!selectedFile || uploading} onClick={handleUpload}>
                {uploading ? '上传中...' : '开始上传'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default KnowledgeBasePage

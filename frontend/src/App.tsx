import { useState, useEffect } from 'react'
import { BrowserRouter as Router, Routes, Route, Link, useLocation, Navigate } from 'react-router-dom'
import ChatPage from './pages/ChatPage'
import PersonalCenterPage from './pages/PersonalCenterPage'
import KnowledgeBasePage from './pages/KnowledgeBasePage'
import HistoryPage from './pages/HistoryPage'
import LoginPage from './pages/LoginPage'
import { ensureAvatarLoaded, resetAvatar, useAvatar } from './services/avatarStore'
import './App.css'

interface User {
  user_id: string
  username: string
}

function SidebarContent({ user, onLogout, onNavClick }: { user: User; onLogout: () => void; onNavClick?: () => void }) {
  const location = useLocation()
  const avatar = useAvatar()

  const handleNavClick = () => {
    onNavClick?.()
  }

  return (
    <>
      <div className="sidebar-header">
        <div className="logo">
          <span className="logo-icon">🤖</span>
          <span className="logo-text">CodePilot</span>
        </div>
        <p className="logo-subtitle">研发知识助手</p>
      </div>
      <nav className="sidebar-nav">
        <Link to="/" className={`nav-item ${location.pathname === '/' ? 'active' : ''}`} onClick={handleNavClick}>
          <span className="nav-icon">💬</span>
          <span className="nav-text">对话</span>
        </Link>
        <Link to="/knowledge" className={`nav-item ${location.pathname === '/knowledge' ? 'active' : ''}`} onClick={handleNavClick}>
          <span className="nav-icon">📚</span>
          <span className="nav-text">知识库</span>
        </Link>
        <Link to="/history" className={`nav-item ${location.pathname === '/history' ? 'active' : ''}`} onClick={handleNavClick}>
          <span className="nav-icon">📋</span>
          <span className="nav-text">历史记录</span>
        </Link>
        <Link to="/profile" className={`nav-item ${location.pathname === '/profile' ? 'active' : ''}`} onClick={handleNavClick}>
          <span className="nav-icon">👤</span>
          <span className="nav-text">个人中心</span>
        </Link>
      </nav>
      <div className="sidebar-footer">
        <div className="user-info">
          {/* 侧边栏头像同样跟随个人中心的上传结果 */}
          <span className="user-avatar">
            {avatar ? <img className="user-avatar-img" src={avatar} alt="我" /> : '👤'}
          </span>
          <span className="user-name">{user.username}</span>
          <button className="btn-logout" onClick={onLogout}>退出</button>
        </div>
      </div>
    </>
  )
}

function App() {
  const [user, setUser] = useState<User | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    const saved = localStorage.getItem('user')
    if (saved) {
      setUser(JSON.parse(saved))
      // 登录态恢复后拉一次头像，供侧边栏与对话气泡共用
      void ensureAvatarLoaded()
    }
  }, [])

  const handleLogin = (u: User, _token: string) => {
    setUser(u)
    void ensureAvatarLoaded(true)
  }

  const handleLogout = () => {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    // 清空头像缓存，避免下一个账号看到上一个账号的头像
    resetAvatar()
    setUser(null)
  }

  if (!user) {
    return <LoginPage onLogin={handleLogin} />
  }

  return (
    <Router>
      <div className="app-layout">
        <div className={`sidebar-overlay ${sidebarOpen ? 'open' : ''}`} onClick={() => setSidebarOpen(false)} />
        <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
          <SidebarContent user={user} onLogout={handleLogout} onNavClick={() => setSidebarOpen(false)} />
        </aside>
        <div className="main-wrapper">
          <div className="mobile-header">
            <button className="menu-btn" onClick={() => setSidebarOpen(!sidebarOpen)}>☰</button>
            <span className="logo-text">CodePilot</span>
            <span className="logo-subtitle">研发知识助手</span>
          </div>
          <main className="main-content">
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/knowledge" element={<KnowledgeBasePage />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/profile" element={<PersonalCenterPage />} />
              <Route path="*" element={<Navigate to="/" />} />
            </Routes>
          </main>
        </div>
      </div>
    </Router>
  )
}

export default App

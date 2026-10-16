import { useState } from 'react'
import { authApi } from '../services/api'
import './LoginPage.css'

interface LoginPageProps {
  onLogin: (user: { user_id: string; username: string }, token: string) => void
}

function LoginPage({ onLogin }: LoginPageProps) {
  const [isRegister, setIsRegister] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const res = isRegister
        ? await authApi.register({ username, password })
        : await authApi.login({ username, password })

      const { token, user_id, username: name } = res.data
      localStorage.setItem('token', token)
      localStorage.setItem('user', JSON.stringify({ user_id, username: name }))
      onLogin({ user_id, username: name }, token)
    } catch (err: any) {
      setError(err.response?.data?.detail || '操作失败，请重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-logo">
          <span className="logo-icon">🤖</span>
          <h1>CodePilot</h1>
          <p>研发知识助手</p>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="请输入用户名"
              required
            />
          </div>
          <div className="form-group">
            <label>密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请输入密码"
              required
            />
          </div>

          {error && <div className="form-error">{error}</div>}

          <button type="submit" className="btn-submit" disabled={loading}>
            {loading ? '请稍候...' : isRegister ? '注册' : '登录'}
          </button>
        </form>

        <div className="login-switch">
          {isRegister ? '已有账号？' : '没有账号？'}
          <button onClick={() => { setIsRegister(!isRegister); setError('') }}>
            {isRegister ? '去登录' : '去注册'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default LoginPage

import React from 'react'
import './AgentTrace.css'

interface AgentTraceProps {
  traces: string[]
}

function AgentTrace({ traces }: AgentTraceProps) {
  if (traces.length === 0) {
    return null
  }

  return (
    <div className="agent-trace-panel">
      <div className="trace-header">
        <span className="trace-icon">🔍</span>
        <span className="trace-title">Agent 执行轨迹</span>
      </div>
      <div className="trace-list">
        {traces.map((trace, index) => (
          <div key={index} className="trace-item">
            <div className="trace-step-indicator">
              <div className={`step-dot ${index === traces.length - 1 ? 'active' : ''}`}></div>
              {index < traces.length - 1 && <div className="step-line"></div>}
            </div>
            <div className="trace-content">
              <span className="step-label">Step {index + 1}</span>
              <span className="step-text">{trace}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default AgentTrace

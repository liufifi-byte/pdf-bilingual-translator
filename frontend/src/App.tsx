import React, { useEffect, useState, useRef } from 'react'
import { Layout, Button, Spin, Typography, message } from 'antd'
import {
  listReports, getReport, getProgress,
  type ReportListItem, type ReportDetail, type ProgressInfo,
} from './api'
import TranslationUpload from './components/TranslationUpload'
import TranslationView from './components/TranslationView'

const { Header, Sider, Content } = Layout
const { Title, Text } = Typography

const PHASE_LABEL: Record<string, string> = {
  queued: '排队中', parsing: '解析 PDF', done: '完成', failed: '失败',
}

const App: React.FC = () => {
  const [view, setView] = useState<{ kind: 'upload' } | { kind: 'detail'; reportId: string }>({
    kind: 'upload',
  })
  const [reports, setReports] = useState<ReportListItem[]>([])
  const [detail, setDetail] = useState<ReportDetail | null>(null)
  const [progress, setProgress] = useState<ProgressInfo | null>(null)
  const pollRef = useRef<number | null>(null)
  const detailRef = useRef<ReportDetail | null>(null)

  const refresh = () => listReports().then(setReports)
  useEffect(() => { refresh() }, [])

  // 选中报告后轮询 (解析完成后停止)
  useEffect(() => {
    if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
    setProgress(null)
    if (detailRef.current && detailRef.current.id !== (view.kind === 'detail' ? view.reportId : null)) {
      setDetail(null); detailRef.current = null
    }
    if (view.kind !== 'detail') return
    const reportId = view.reportId

    const poll = async () => {
      try {
        const p = await getProgress(reportId)
        setProgress(p)
        if (!detailRef.current) {
          const d = await getReport(reportId)
          detailRef.current = d
          setDetail(d)
        }
        if (p.status === 'done' || p.status === 'failed') {
          if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
          if (p.status === 'failed') message.error(`处理失败: ${p.error || p.msg}`)
          refresh()
        }
      } catch (e) {
        if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
      }
    }
    poll()
    pollRef.current = window.setInterval(poll, 2000)
    return () => { if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null } }
  }, [view])

  const handleUploaded = (reportId: string) => {
    setView({ kind: 'detail', reportId })
    refresh()
  }

  const showProgress = progress && progress.status !== 'done' && progress.status !== 'failed'
    && view.kind === 'detail'

  return (
    <Layout className="u-app-layout" style={{ minHeight: '100vh' }}>
      <Header className="u-app-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span className="u-status-dot u-status-dot-active" />
          <Title level={4}>PDF 双语翻译</Title>
        </div>
        {progress && view.kind === 'detail' && (
          <div style={{ color: '#94a3b8', fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>{PHASE_LABEL[progress.phase] || progress.status}</span>
            {progress.total > 0 && <span className="text-mono">{progress.current}/{progress.total}</span>}
          </div>
        )}
      </Header>

      <Layout>
        <Sider width={220} theme="light" className="u-app-sider">
          <div className="text-caption" style={{ marginBottom: 8, fontSize: 11, textTransform: 'uppercase' }}>
            历史文档
          </div>
          {reports.length === 0 ? (
            <div className="text-caption" style={{ padding: '4px 12px', fontSize: 12 }}>
              暂无
            </div>
          ) : (
            reports.map(r => {
              const active = view.kind === 'detail' && view.reportId === r.id
              return (
                <div
                  key={r.id}
                  className={`u-report-item ${active ? 'u-report-item-active' : ''}`}
                  onClick={() => {
                    setDetail(null); detailRef.current = null; setProgress(null)
                    setView({ kind: 'detail', reportId: r.id })
                  }}
                  style={{ fontSize: 12 }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      maxWidth: 130,
                    }}>
                      {r.file_name}
                    </span>
                    <span className={`u-status-dot u-status-dot-${r.status === 'done' ? 'done' : r.status === 'failed' ? 'failed' : 'active'}`} />
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 2 }}>
                    {new Date(r.upload_time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                  </div>
                </div>
              )
            })
          )}
        </Sider>

        <Content className="u-app-content">
          {view.kind === 'upload' && <TranslationUpload onUploaded={handleUploaded} />}

          {view.kind === 'detail' && (
            <div style={{ maxWidth: 1100, margin: '0 auto' }}>
              <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 12 }}>
                <Button onClick={() => {
                  setDetail(null); detailRef.current = null; setProgress(null)
                  setView({ kind: 'upload' })
                }}>
                  ← 返回
                </Button>
                <Text style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
                  {detail?.file_name}
                </Text>
              </div>

              {showProgress && (
                <div className="u-card" style={{ marginBottom: 16, padding: 16 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                    <span className="u-status-dot u-status-dot-active" />
                    <Text strong style={{ fontSize: 14 }}>
                      {PHASE_LABEL[progress!.phase] || progress!.phase}
                    </Text>
                    <span style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{progress!.msg}</span>
                  </div>
                </div>
              )}

              {!detail ? (
                <div style={{ textAlign: 'center', padding: 80 }}>
                  <Spin />
                </div>
              ) : (
                <TranslationView detail={detail} />
              )}
            </div>
          )}
        </Content>
      </Layout>
    </Layout>
  )
}

export default App

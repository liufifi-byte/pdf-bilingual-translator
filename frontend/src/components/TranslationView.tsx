import React, { useEffect, useRef, useState } from 'react'
import { Typography, Button, Tabs, Spin, message, Progress, Modal, Input, Select } from 'antd'
import { DownloadOutlined, FileTextOutlined, BookOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import * as pdfjsLib from 'pdfjs-dist'
import PdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?worker'
import {
  pdfFileUrl, getTranslationProgress, startTranslation,
  startFullTranslation, getFullTranslationProgress, bilingualPdfDownloadUrl,
  requestPageTranslation, getPageTranslationStream,
  detectSourceLanguage, getAiSummary,
  listGlossary, addGlossary, updateGlossary, deleteGlossary,
  type ReportDetail, type GlossaryEntry, type OverlayBlockMeta,
} from '../api'

const { Text } = Typography
pdfjsLib.GlobalWorkerOptions.workerPort = new PdfWorker()

type PageStatus = 'idle' | 'loading' | 'done' | 'empty'
// 全文翻译+下载的状态机
type DownloadState = 'idle' | 'translating' | 'rendering' | 'ready' | 'failed'

const intToCssColor = (n: number) => `#${(n & 0xffffff).toString(16).padStart(6, '0')}`

/** 单条浮层块: 已揭示 → 白底译文 (标题档位 + 字号自适应缩到装下); 未揭示 → 半透明置灰
 *  标题档位与后端 PDF 渲染器同规则: 粗体短块或字号显著大 = 标题 → 粗体+放大
 *  (不低于正文档+1); 小号粗标签/多行合并块保持原字号; 正文归一到主导字号。 */
const OverlayBlock: React.FC<{
  meta: OverlayBlockMeta; target?: string; scale: number; bodySize?: number | null
  bg?: string
}> = ({ meta, target, scale, bodySize, bg }) => {
  const ref = useRef<HTMLDivElement>(null)
  const orig = meta.font_size || 10
  const body = bodySize ?? 10
  const isHeading = (meta.bold && (meta.len ?? 40) <= 120) || orig > body + 1.2
  let baseFs = orig
  if (isHeading) {
    if (orig < body - 0.5 || (meta.lines ?? 1) >= 2) {
      baseFs = orig           // 小号粗标签 / 多行合并块: 保持原字号不放大
    } else {
      baseFs = Math.max(orig, body + 1)   // 真标题: 加粗 + 至少正文档+1
    }
  } else if (Math.abs(orig - body) < 1.2) {
    baseFs = body             // 正文: 主导字号档
  }
  // 标题默认行距 1.15 (PDF 渲染器用原文行距, 标题通常 ≈1.0-1.1; 1.28 占高
  // 多 20% 会触发多余缩字号); 收紧阶梯对齐 PDF: 先收行距 (至 1.02) 再降
  // 字号 (0.25/档), 且允许末行下沉 0.3em (PDF 允许 0.5fs 基线沉降)
  const [fit, setFit] = useState<{ fs: number; lh: number }>(
    { fs: baseFs * scale, lh: isHeading ? 1.15 : 1.28 })
  useEffect(() => {
    if (!target || !ref.current) return
    const el = ref.current
    let fs = baseFs * scale
    let lh = isHeading ? 1.15 : 1.28
    let tol = fs * 0.3
    el.style.fontSize = `${fs}px`
    el.style.lineHeight = `${lh}`
    let guard = 0
    while (guard++ < 44 &&
           (el.scrollWidth > el.clientWidth + 1 ||
            el.scrollHeight > el.clientHeight + 1 + tol)) {
      if (lh > 1.03) { lh = Math.max(1.02, lh - 0.08); el.style.lineHeight = `${lh}`; continue }
      if (fs > 5) { fs -= 0.25; tol = fs * 0.3; el.style.fontSize = `${fs}px`; continue }
      break
    }
    setFit({ fs, lh })
  }, [target, scale, baseFs, isHeading]) // eslint-disable-line
  const fontSize = fit.fs
  const cssColor = intToCssColor(meta.color ?? 0)
  const [x0, y0, x1, y1] = meta.bbox
  const style: React.CSSProperties = {
    position: 'absolute',
    left: x0 * scale, top: y0 * scale,
    width: (x1 - x0) * scale, height: (y1 - y0) * scale,
    boxSizing: 'border-box',
    transition: 'background-color .35s ease',
  }
  if (!target) {
    // 未翻译: 置灰 (原文仍透出, 明确"这段还没好")
    return <div style={{ ...style, background: 'rgba(100,116,139,0.38)', borderRadius: 1 }} />
  }
  return (
    <div ref={ref} style={{
      ...style,
      background: bg || '#fff',   // 原块底色 (画布采样): 黄色高亮框等保留本色
      padding: '1px 2px',
      overflow: 'hidden',
      fontFamily: '"Songti SC","STSong","SimSun",serif',
      fontSize, lineHeight: fit.lh,
      color: cssColor,
      fontWeight: isHeading ? 700 : 400,
      // 标题描边加粗: PDF 版 render_mode=2 (border 0.055×fs, 墨量≈2.3×),
      // 纯 CSS 700 在宋体上明显偏轻 → 叠细描边补墨
      ...(isHeading ? { WebkitTextStroke: `0.022em ${cssColor}` } : {}),
      fontStyle: meta.italic ? 'italic' : 'normal',
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    }}>{target}</div>
  )
}

/** 流式译页: 原文画布只渲染一次, 轮询拉译文 → 文章区域内段落连续流动,
 *  未译段落置灰占位, 译好的段落按阅读顺序逐个"点亮", 页面体感只刷新一次。 */
const StreamTranslatedPage: React.FC<{
  reportId: string; pageNum: number; source: string; pdfUrl: string;
  tgtLang: string; onDone?: () => void;
}> = ({ reportId, pageNum, source, pdfUrl, tgtLang, onDone }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [vp, setVp] = useState<{ w: number; h: number; scale: number } | null>(null)
  const [dispScale, setDispScale] = useState(1)
  const [metas, setMetas] = useState<OverlayBlockMeta[]>([])
  const [bodySize, setBodySize] = useState<number | null>(null)
  const [translated, setTranslated] = useState<Record<string, string>>({})
  const [pageDone, setPageDone] = useState(false)
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone

  // 画布: 渲染原文一次 (不随翻译刷新)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const pdf = await pdfjsLib.getDocument({
          url: pdfFileUrl(pdfUrl), cMapUrl: '/cmaps/', cMapPacked: true,
        }).promise
        if (cancelled) return
        const page = await pdf.getPage(pageNum)
        const scale = 1.3
        const viewport = page.getViewport({ scale })
        const canvas = canvasRef.current
        if (!canvas) return
        canvas.width = viewport.width
        canvas.height = viewport.height
        const ctx = canvas.getContext('2d')
        if (!ctx) return
        await page.render({ canvasContext: ctx, viewport } as any).promise
        if (!cancelled) setVp({ w: viewport.width, h: viewport.height, scale })
      } catch (e) { console.error('[stream page] 原文渲染失败:', e) }
    })()
    return () => { cancelled = true }
  }, [pdfUrl, pageNum])

  // 适配面板宽: 画布+区域整体 transform 等比缩放 (坐标系一致, 不错位不超宽)
  React.useLayoutEffect(() => {
    if (!vp || !wrapRef.current) return
    const el = wrapRef.current
    const update = () => {
      const w = el.clientWidth
      if (w > 0) setDispScale(Math.min(1, w / vp.w))
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [vp])

  // 块底色采样: 画布渲染后取每块四角+边中点 8 像素众数色 (白块=白,
  // 黄色高亮框=黄) → 译文浮层用本色底, 不再把彩色底涂白
  const [bgMap, setBgMap] = useState<Record<string, string>>({})
  useEffect(() => {
    if (!vp || !metas.length) return
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { willReadFrequently: true } as any) as CanvasRenderingContext2D | null
    if (!ctx) return
    const map: Record<string, string> = {}
    for (const m of metas) {
      const [x0, y0, x1, y1] = m.bbox
      const xs = [x0 + 3, (x0 + x1) / 2, x1 - 3]
      const ys = [y0 + 3, (y0 + y1) / 2, y1 - 3]
      const pts: Array<[number, number]> = [
        [xs[0], ys[0]], [xs[1], ys[0]], [xs[2], ys[0]],
        [xs[0], ys[1]], [xs[2], ys[1]],
        [xs[0], ys[2]], [xs[1], ys[2]], [xs[2], ys[2]],
      ]
      const counts: Record<string, number> = {}
      let best = '', bestN = 0
      try {
        for (const [x, y] of pts) {
          const d = ctx.getImageData(Math.round(x * vp.scale), Math.round(y * vp.scale), 1, 1).data
          const key = `${d[0]},${d[1]},${d[2]}`
          counts[key] = (counts[key] || 0) + 1
          if (counts[key] > bestN) { bestN = counts[key]; best = key }
        }
      } catch { /* 跨域画布保护等异常 → 回退白底 */ }
      if (best) map[m.block_id] = `rgb(${best})`
    }
    setBgMap(map)
  }, [vp, metas])

  // 轮询: 1s 拉译文; done 后停止 (tgt_lang 跟随用户选择, 换语言时换方向重译)
  useEffect(() => {
    let stop = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const tick = async () => {
      try {
        const r = await getPageTranslationStream(reportId, pageNum, source, 'auto', tgtLang)
        if (stop) return
        setMetas(r.blocks || [])
        setBodySize(r.body_size ?? null)
        setTranslated(r.translated || {})
        if (r.done) { setPageDone(true); onDoneRef.current?.(); return }
      } catch (e) { /* 单次失败忽略, 下轮重试 */ }
      timer = setTimeout(tick, 1000)
    }
    tick()
    return () => { stop = true; if (timer) clearTimeout(timer) }
  }, [reportId, pageNum, source, tgtLang])

  // 揭示策略: 翻译中只揭示"前缀" (前序全好了才亮, 视觉=从上到下逐段翻);
  // 页完成后全部揭示 (个别失败块不再卡住后序)
  let revealCount = 0
  for (const m of metas) {
    if (translated[m.block_id]) revealCount++
    else break
  }
  if (pageDone) revealCount = metas.length

  return (
    // 外层: 占满面板宽、高度按缩放比收窄; 内层: 定宽 vp.w 像素坐标系
    // (画布+区域同系), 整体 transform 缩放 → 对齐且不超出面板
    <div ref={wrapRef} style={{
      width: '100%',
      height: vp ? `${vp.h * dispScale}px` : undefined,
      overflow: 'hidden',
    }}>
      <div style={{
        position: 'relative',
        width: vp ? `${vp.w}px` : '100%',
        height: vp ? `${vp.h}px` : undefined,
        transform: dispScale < 1 ? `scale(${dispScale})` : undefined,
        transformOrigin: 'top left',
      }}>
        <canvas ref={canvasRef} style={{ display: 'block' }} />
        {vp && metas.map((m, i) => (
          <OverlayBlock key={m.block_id} meta={m}
            target={i < revealCount ? translated[m.block_id] : undefined}
            scale={vp.scale} bodySize={bodySize} bg={bgMap[m.block_id]} />
        ))}
      </div>
    </div>
  )
}

interface SourcePanelProps {
  reportId: string
  fileName: string
  source: string
  label: string
}

/** 目标语言可选项 (需求: 中英韩日西俄); 源语言始终自动检测 */
const TGT_LANG_OPTIONS = ['中文', '英文', '韩文', '日文', '西班牙文', '俄文']

const SourcePanel: React.FC<SourcePanelProps> = ({ reportId, fileName, source, label }) => {
  const [numPages, setNumPages] = useState(0)
  const [pageNums, setPageNums] = useState<number[]>([])
  const [translateStarted, setTranslateStarted] = useState(false)
  const [pageStatus, setPageStatus] = useState<Record<number, PageStatus>>({})
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  // 全文翻译 + 下载状态
  const [downloadState, setDownloadState] = useState<DownloadState>('idle')
  const [downloadPct, setDownloadPct] = useState(0)
  const [downloadMsg, setDownloadMsg] = useState('')
  const downloadPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ─── 语言选择 + AI 概括相关 state ───
  // 源语言自动检测 (srcLang 固定 'auto', 检测结果仅展示); 目标语言用户选
  const [tgtLang, setTgtLang] = useState('中文')
  const [srcLangDetected, setSrcLangDetected] = useState<string | null>(null)
  // AI 概括 (顶部按钮触发, 后端按 report+source 缓存)
  const [aiSummary, setAiSummary] = useState<string | null>(null)
  const [aiLoading, setAiLoading] = useState(false)

  // 挂载时检测源文档语言 → 「自动检测(英文)」框展示。
  // 新上传的文档解析需几十秒, 400 时每 5s 重试 (最多 1 分钟), 解析完自动补上
  useEffect(() => {
    let cancelled = false
    let tries = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    const attempt = async () => {
      try {
        const r = await detectSourceLanguage(reportId, source)
        if (!cancelled) setSrcLangDetected(r.src_lang)
      } catch {
        if (!cancelled && tries++ < 12) timer = setTimeout(attempt, 5000)
      }
    }
    attempt()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [reportId, source])

  // ─── 术语库相关 state ───
  // hasPendingGlossaryChange: 用户改/增/删过任意术语, 且本轮还没全消化
  // retranslatedThisRound: 本轮已经本地重译过的页 (避免重复提示)
  const [hasPendingGlossaryChange, setHasPendingGlossaryChange] = useState(false)
  const retransSetRef = useRef<Set<number>>(new Set())
  // 当前可见页 (IntersectionObserver 更新) — 用于在「主内容白色卡片最底端」显示重译提示
  const [currentPage, setCurrentPage] = useState(1)

  // 术语库 Modal state
  const [glossaryOpen, setGlossaryOpen] = useState(false)
  const [glossaryEntries, setGlossaryEntries] = useState<GlossaryEntry[]>([])
  const [glossaryLoading, setGlossaryLoading] = useState(false)
  const [newEntryEn, setNewEntryEn] = useState('')
  const [newEntryZh, setNewEntryZh] = useState('')
  const [addingRow, setAddingRow] = useState(false)
  const [editingKey, setEditingKey] = useState<string | null>(null)   // en of row being edited
  const [editingZh, setEditingZh] = useState('')

  const leftRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const rightRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const renderedRef = useRef<Set<number>>(new Set())
  const requestedRef = useRef<Set<number>>(new Set())
  const statusRef = useRef<Record<number, PageStatus>>({})

  const pdfUrl = `${reportId}_${fileName}`

  // 加载左侧原文 PDF
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const pdf = await pdfjsLib.getDocument({
          url: pdfFileUrl(pdfUrl), cMapUrl: '/cmaps/', cMapPacked: true,
        }).promise
        if (cancelled) return
        const total = pdf.numPages
        setNumPages(total)
        setPageNums(Array.from({ length: total }, (_, i) => i + 1))
        const initStatus: Record<number, PageStatus> = {}
        for (let i = 1; i <= total; i++) initStatus[i] = 'idle'
        setPageStatus(initStatus)
        statusRef.current = { ...initStatus }

        for (let p = 1; p <= total; p++) {
          if (cancelled || renderedRef.current.has(p)) continue
          const page = await pdf.getPage(p)
          const vp = page.getViewport({ scale: 1.3 })
          const canvas = leftRefs.current[p]?.querySelector('canvas') as HTMLCanvasElement | null
          if (canvas) {
            canvas.width = vp.width; canvas.height = vp.height
            const ctx = canvas.getContext('2d')!
            await page.render({ canvasContext: ctx, viewport: vp } as any).promise
            renderedRef.current.add(p)
          }
        }
      } catch (e) { console.error('原文 PDF 加载失败:', e) }
    }
    load()
    return () => { cancelled = true }
  }, [pdfUrl])

  // 翻译启动后: 立即请求第 1 页 + IntersectionObserver 监听后续页
  // (tgtLang 进依赖: 换目标语言后重挂观察器, 可见页按新语言重新请求)
  useEffect(() => {
    if (!translateStarted || numPages === 0) return
    _requestBilingualPage(1)

    const observer = new IntersectionObserver((entries) => {
      // 取最靠上的可见页作 currentPage
      let topMost: number | null = null
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return
        const p = Number((entry.target as HTMLElement).dataset.pageId)
        if (!p) return
        if (topMost === null || p < topMost) topMost = p
        const st = statusRef.current[p]
        if (st === 'done' || st === 'loading' || st === 'empty') return
        _requestBilingualPage(p)
      })
      if (topMost !== null) setCurrentPage(topMost)
    }, { root: null, rootMargin: '200px 0px', threshold: 0.05 })

    pageNums.forEach(p => {
      // 同时监听左右两侧 ref (确保左侧滑到也触发)
      const elR = rightRefs.current[p]
      if (elR) observer.observe(elR)
    })
    return () => observer.disconnect()
  }, [translateStarted, numPages, tgtLang]) // eslint-disable-line

  const _requestBilingualPage = (p: number) => {
    if (requestedRef.current.has(p)) return
    const st = statusRef.current[p]
    if (st === 'done' || st === 'loading' || st === 'empty') return
    requestedRef.current.add(p)
    _setStatus(p, 'loading')
    // StreamTranslatedPage 挂载后自行驱动: 原文画布渲染一次 + 1s 轮询译文,
    // 未译块置灰、译好的块按阅读顺序逐个点亮, 完成时回调置 done
  }

  const _setStatus = (p: number, st: PageStatus) => {
    setPageStatus(prev => ({ ...prev, [p]: st }))
    statusRef.current[p] = st
  }

  // 进度轮询
  useEffect(() => {
    if (!translateStarted) return
    const timer = setInterval(async () => {
      try {
        const prog = await getTranslationProgress(reportId, source)
        setProgress({ done: prog.done, total: prog.total })
      } catch (e) { /* ignore */ }
    }, 3000)
    return () => clearInterval(timer)
  }, [reportId, source, translateStarted]) // eslint-disable-line

  const handleStart = async () => {
    try {
      await startTranslation(reportId, source, 'auto', tgtLang)
      message.success(`${label} 翻译已启动 (${srcLangDetedLabel()} → ${tgtLang})`)
      setTranslateStarted(true)
    } catch (e: any) {
      message.error(`启动失败: ${e?.message}`)
    }
  }

  // 源语言展示文案: 已检测出 → 「自动检测(英文)」, 未检出 → 「自动检测」
  const srcLangDetedLabel = () =>
    srcLangDetected ? `自动检测(${srcLangDetected})` : '自动检测'

  /** 换目标语言: 后端 _ensure_lang 清旧缓存, 前端同步重置逐页状态 + 下载态。 */
  const handleTgtLangChange = (v: string) => {
    if (v === tgtLang) return
    setTgtLang(v)
    requestedRef.current.clear()
    retransSetRef.current.clear()
    setHasPendingGlossaryChange(false)
    const init: Record<number, PageStatus> = {}
    for (let i = 1; i <= numPages; i++) init[i] = 'idle'
    setPageStatus(init)
    statusRef.current = { ...init }
    setProgress({ done: 0, total: numPages })
    if (downloadPollRef.current) { clearInterval(downloadPollRef.current); downloadPollRef.current = null }
    setDownloadState('idle')
    setDownloadMsg(''); setDownloadPct(0)
    if (translateStarted) message.info(`已切换目标语言: ${tgtLang} → ${v}, 可见页将按新语言重译`)
  }

  /** AI 概括: refresh=false 命中后端缓存秒回, true 强制重新生成。 */
  const handleAiSummary = async (refresh: boolean) => {
    if (aiLoading) return
    setAiLoading(true)
    try {
      const r = await getAiSummary(reportId, source, refresh)
      setAiSummary(r.summary)
    } catch (e: any) {
      message.error(`AI 概括生成失败: ${e?.response?.data?.detail || e?.message}`)
    } finally {
      setAiLoading(false)
    }
  }

  // 清理轮询
  useEffect(() => {
    return () => {
      if (downloadPollRef.current) clearInterval(downloadPollRef.current)
    }
  }, [])

  // 触发全文翻译 + 完成后自动下载
  const handleDownloadBilingual = async () => {
    if (downloadState === 'translating' || downloadState === 'rendering') return
    setDownloadState('translating')
    setDownloadMsg('正在准备...')
    setDownloadPct(0)
    try {
      const r = await startFullTranslation(reportId, source, 'auto', tgtLang)
      if (r.state === 'ready') {
        // 后端已有现成译文 PDF, 直接下载
        setDownloadState('ready')
        setDownloadMsg('译文 PDF 已就绪')
        window.location.href = bilingualPdfDownloadUrl(reportId, source)
        return
      }
      // 否则轮询全文翻译进度
      const total = r.total_pages || r.total || numPages || 1
      downloadPollRef.current = setInterval(async () => {
        try {
          const p = await getFullTranslationProgress(reportId, source)
          const pct = p.total > 0 ? Math.round((p.current / p.total) * 100) : 0
          setDownloadPct(pct)
          setDownloadMsg(p.msg || '')
          if (p.bilingual_pdf_ready || p.state === 'ready' || p.state === 'done') {
            // 完成 → 停轮询 + 触发下载
            if (downloadPollRef.current) clearInterval(downloadPollRef.current)
            setDownloadState('ready')
            setDownloadMsg('译文 PDF 已就绪')
            setDownloadPct(100)
            message.success('译文 PDF 已生成, 开始下载')
            window.location.href = bilingualPdfDownloadUrl(reportId, source)
          } else if (p.state === 'failed') {
            if (downloadPollRef.current) clearInterval(downloadPollRef.current)
            setDownloadState('failed')
            setDownloadMsg(p.msg || '翻译失败')
            message.error('译文生成失败: ' + (p.msg || '未知错误'))
          } else {
            setDownloadState(p.state === 'rendering' ? 'rendering' : 'translating')
          }
        } catch (e) { /* 单次轮询失败忽略 */ }
      }, 2000)
    } catch (e: any) {
      setDownloadState('failed')
      setDownloadMsg(`启动失败: ${e?.message}`)
      message.error(`下载译文失败: ${e?.message}`)
    }
  }

  // ─── 术语库 Modal 操作 ───
  const openGlossary = async () => {
    setGlossaryOpen(true)
    setAddingRow(false)
    setNewEntryEn(''); setNewEntryZh('')
    setEditingKey(null)
    await refreshGlossary()
  }
  const refreshGlossary = async () => {
    setGlossaryLoading(true)
    try {
      const items = await listGlossary()
      setGlossaryEntries(items)
    } catch (e: any) {
      message.error(`加载术语库失败: ${e?.message}`)
    } finally {
      setGlossaryLoading(false)
    }
  }
  const handleAddEntry = async () => {
    const en = newEntryEn.trim(); const zh = newEntryZh.trim()
    if (!en || !zh) { message.warning('英文原文和标准译法都必填'); return }
    try {
      await addGlossary(en, zh)
      setNewEntryEn(''); setNewEntryZh('')
      setAddingRow(false)
      await refreshGlossary()
      markGlossaryChanged()
      message.success(`已新增术语: ${en} → ${zh}`)
    } catch (e: any) {
      const status = e?.response?.status
      if (status === 409) message.error('术语已存在')
      else message.error(`新增失败: ${e?.message}`)
    }
  }
  const handleSaveEdit = async (en: string) => {
    const zh = editingZh.trim()
    if (!zh) { message.warning('标准译法不能为空'); return }
    try {
      await updateGlossary(en, zh)
      setEditingKey(null); setEditingZh('')
      await refreshGlossary()
      markGlossaryChanged()
      message.success(`已更新术语: ${en}`)
    } catch (e: any) {
      message.error(`更新失败: ${e?.message}`)
    }
  }
  const handleDeleteEntry = async (en: string) => {
    try {
      await deleteGlossary(en)
      await refreshGlossary()
      markGlossaryChanged()
      message.success(`已删除术语: ${en}`)
    } catch (e: any) {
      message.error(`删除失败: ${e?.message}`)
    }
  }
  /** 标记术语已变更, 同时清空本轮已重译集合 (因为所有页都需要重新提示). */
  const markGlossaryChanged = () => {
    setHasPendingGlossaryChange(true)
    retransSetRef.current.clear()
  }

  /** 用户点击右下角「点此重译」: 调 force=true 重译当前页, 完成后重新挂载流式页. */
  const handleRetranslateVisiblePage = async () => {
    const p = currentPage
    if (!p) return
    _setStatus(p, 'loading')
    try {
      await requestPageTranslation(reportId, p, source, /* force */ true, 'auto', tgtLang)
      // force 重译在后端同步完成后 → 卸载旧浮层页重新挂载 (拉新缓存)
      requestedRef.current.delete(p)
      _setStatus(p, 'idle')
      setTimeout(() => { _requestBilingualPage(p) }, 100)
      // 标记该页本轮已重译, 移除该页提示
      retransSetRef.current.add(p)
    } catch (e: any) {
      message.error(`重译第 ${p} 页失败: ${e?.message}`)
      _setStatus(p, 'done')   // 回退到原状态
    }
  }

  const pct = numPages > 0 ? Math.round((progress.done / numPages) * 100) : 0

  // 当前可见页是否应显示重译角标
  const shouldShowRetranslateTip =
    translateStarted
    && hasPendingGlossaryChange
    && !retransSetRef.current.has(currentPage)
    && (pageStatus[currentPage] === 'done')

  return (
    <div>
      {/* 顶部卡片: 进度/语言选择 + 右上角 AI概括 & 术语库入口 */}
      <div className="u-card" style={{ marginBottom: 16, padding: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
          <span className={`u-status-dot ${translateStarted ? 'u-status-dot-active' : 'u-status-dot-idle'}`} />
          {translateStarted
            ? <Text strong style={{ fontSize: 13 }}>已译 {progress.done} / {numPages} 页 · 滑到哪页译哪页</Text>
            : <Text strong style={{ fontSize: 13 }}>选择目标语言后开始翻译</Text>}
          {translateStarted && (
            <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }} className="text-mono">
              {pct}%
            </span>
          )}
          {/* 右上角操作区: AI 概括 + 术语库 (术语库从页面底部移入) */}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={aiLoading}
              onClick={() => handleAiSummary(false)}
              style={{ color: 'var(--accent-primary)', borderColor: 'var(--accent-primary)' }}
            >
              AI 概括
            </Button>
            <Button
              size="small"
              icon={<BookOutlined />}
              onClick={openGlossary}
              style={{ color: 'var(--accent-primary)', borderColor: 'var(--accent-primary)' }}
            >
              术语库
            </Button>
          </div>
        </div>

        {/* 语言选择行: 源语言自动检测 (只读展示), 目标语言可选 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Text style={{ fontSize: 12, color: 'var(--text-secondary)' }}>翻译方向</Text>
          <Select
            size="small"
            value="auto"
            disabled
            style={{ width: 150 }}
            options={[{ value: 'auto', label: srcLangDetedLabel() }]}
          />
          <span style={{ color: 'var(--text-tertiary)' }}>→</span>
          <Select
            size="small"
            value={tgtLang}
            onChange={handleTgtLangChange}
            style={{ width: 110 }}
            options={TGT_LANG_OPTIONS.map(l => ({ value: l, label: l }))}
          />
          {!translateStarted && (
            <Button type="primary" onClick={handleStart} style={{ marginLeft: 8 }}>
              开始翻译 {label}
            </Button>
          )}
        </div>

        {translateStarted && (
          <div className="u-progress-bar" style={{ marginTop: 10 }}>
            <div className="u-progress-bar-fill" style={{ width: `${pct}%` }} />
          </div>
        )}
      </div>

      {/* AI 概括结果卡片 (点顶部「AI 概括」按钮后展开) */}
      {aiSummary !== null && (
        <div className="u-card" style={{ marginBottom: 16, padding: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <ThunderboltOutlined style={{ color: 'var(--accent-primary)' }} />
            <Text strong style={{ fontSize: 13 }}>AI 概括</Text>
            <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
              {srcLangDetected ? `源文档语言: ${srcLangDetected}` : ''}
            </span>
            <div style={{ marginLeft: 'auto' }}>
              <a style={{ color: 'var(--accent-primary)', fontSize: 12 }}
                 onClick={() => handleAiSummary(true)}>重新生成</a>
              <span style={{ color: 'var(--text-tertiary)', margin: '0 6px', fontSize: 12 }}>|</span>
              <a style={{ color: 'var(--text-tertiary)', fontSize: 12 }}
                 onClick={() => setAiSummary(null)}>收起</a>
            </div>
          </div>
          <div style={{ fontSize: 13, lineHeight: 1.8, whiteSpace: 'pre-wrap' }}>
            {aiSummary}
          </div>
        </div>
      )}

      {/* 左原文 / 右按页译 PDF */}
      {pageNums.map(p => {
        const st = pageStatus[p] || 'idle'
        return (
          <div key={p} className="u-page-pair">
            {/* 左: 原文 */}
            <div ref={(el) => { leftRefs.current[p] = el }} data-page-id={p} className="u-page-pane">
              <div className="u-page-pane-header"><span>第 {p} 页 · 原文</span></div>
              <div className="u-page-pane-body"><canvas /></div>
            </div>
            {/* 右: 单页译 PDF (渲染保排版的中文) */}
            <div ref={(el) => { rightRefs.current[p] = el }} data-page-id={p} className="u-page-pane">
              <div className="u-page-pane-header">
                <span>第 {p} 页 · 译文</span>
                {st === 'done' && <span style={{ color: 'var(--data-up)' }}>●</span>}
                {st === 'loading' && <span style={{ color: 'var(--accent-primary)' }}>翻译中…</span>}
                {!translateStarted && <span style={{ color: 'var(--text-tertiary)' }}>未开始</span>}
                {translateStarted && st === 'idle' && <span style={{ color: 'var(--text-tertiary)' }}>待翻译</span>}
              </div>
              <div className="u-page-pane-body" style={{ minHeight: 200 }}>
                {!translateStarted && <div className="text-caption">点击「开始翻译」</div>}
                {translateStarted && st === 'idle' && <div className="text-caption">滑到此处自动翻译</div>}
                {st === 'loading' && (
                  <>
                    <div className="u-skeleton-line" />
                    <div className="u-skeleton-line" />
                    <div className="u-skeleton-line" style={{ width: '70%' }} />
                  </>
                )}
                {(st === 'loading' || st === 'done') && (
                  <StreamTranslatedPage
                    key={`stream-${p}-${tgtLang}`}
                    reportId={reportId}
                    pageNum={p}
                    source={source}
                    pdfUrl={pdfUrl}
                    tgtLang={tgtLang}
                    onDone={() => { _setStatus(p, 'done') }}
                  />
                )}
              </div>
            </div>
          </div>
        )
      })}

      {/* 下载译文 PDF (术语库入口已移至页面顶部右上角) */}
      <div className="u-card" style={{ marginTop: 16, padding: 12 }}>
        {(() => {
          const ds: DownloadState = downloadState
          const busy = ds === 'translating' || ds === 'rendering'
          if (busy) {
            return (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                  <Spin size="small" />
                  <Text strong style={{ fontSize: 13 }}>
                    {ds === 'rendering' ? '正在合并译文 PDF...' : `正在全文翻译 ${downloadPct}%`}
                  </Text>
                  <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-tertiary)' }} className="text-mono">
                    {downloadPct}%
                  </span>
                </div>
                <Progress percent={downloadPct} size="small" status="active" />
                {downloadMsg && (
                  <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 4 }}>
                    {downloadMsg} · 翻完所有页后会自动开始下载
                  </div>
                )}
              </>
            )
          }
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <FileTextOutlined style={{ color: 'var(--accent-primary)' }} />
              <div style={{ flex: 1 }}>
                <Text strong style={{ fontSize: 13 }}>
                  {ds === 'ready' ? '译文 PDF 已生成, 可重新下载' : '下载完整译文报告（PDF）'}
                </Text>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                  {ds === 'failed'
                    ? downloadMsg || '翻译失败'
                    : '会自动翻译未译页面 (约 1-3 分钟), 完成后自动下载'}
                </div>
              </div>
              {/* 下载按钮 (术语库按钮已移至顶部右上角) */}
              <Button
                type={ds === 'ready' ? 'default' : 'primary'}
                icon={<DownloadOutlined />}
                loading={busy}
                disabled={busy}
                onClick={handleDownloadBilingual}
              >
                {ds === 'ready' ? '重新下载' : '下载报告'}
              </Button>
            </div>
          )
        })()}
      </div>

      {/* 术语更新底部提示 (用户规范: 卡片底端, 居中小字号) */}
      {shouldShowRetranslateTip && (
        <div style={{
          textAlign: 'center',
          marginTop: 12,
          padding: '8px 0',
          fontSize: 12,
          color: 'var(--text-secondary)',
        }}>
          <ReloadOutlined style={{ color: 'var(--data-up)', marginRight: 6 }} />
          <span>术语已更新，</span>
          {pageStatus[currentPage] === 'loading'
            ? <span style={{ color: 'var(--accent-primary)' }}>重译中…</span>
            : <a style={{ color: 'var(--accent-primary)' }}
                  onClick={handleRetranslateVisiblePage}>点此重译</a>}
        </div>
      )}

      {/* 术语库 Modal */}
      <Modal
        open={glossaryOpen}
        onCancel={() => setGlossaryOpen(false)}
        footer={null}
        width={620}
        title={
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span><BookOutlined style={{ marginRight: 8 }} />术语库</span>
            <Button
              size="small"
              type="primary"
              onClick={() => { setAddingRow(true); setNewEntryEn(''); setNewEntryZh('') }}
              disabled={addingRow}
            >
              + 新增翻译
            </Button>
          </div>
        }
      >
        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>
          修改/新增的术语会在下次翻译时自动注入；已译过的页面右下角提示可重译。
        </div>
        <Spin spinning={glossaryLoading}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: 'var(--bg-sunken, #f5f5f5)' }}>
                <th style={{ textAlign: 'left', padding: '8px 10px', borderBottom: '1px solid var(--border-subtle, #eee)', fontWeight: 600 }}>英文原文</th>
                <th style={{ textAlign: 'left', padding: '8px 10px', borderBottom: '1px solid var(--border-subtle, #eee)', fontWeight: 600 }}>标准译法</th>
                <th style={{ textAlign: 'right', padding: '8px 10px', borderBottom: '1px solid var(--border-subtle, #eee)', fontWeight: 600 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {glossaryEntries.length === 0 && !addingRow && (
                <tr>
                  <td colSpan={3} style={{ textAlign: 'center', padding: 20, color: 'var(--text-tertiary)' }}>
                    暂无术语，点右上「+ 新增翻译」添加
                  </td>
                </tr>
              )}
              {glossaryEntries.map(g => (
                <tr key={g.en} style={{ borderBottom: '1px solid var(--border-subtle, #f0f0f0)' }}>
                  <td style={{ padding: '8px 10px' }}>{g.en}</td>
                  <td style={{ padding: '8px 10px' }}>
                    {editingKey === g.en
                      ? <Input size="small" value={editingZh} onChange={e => setEditingZh(e.target.value)} style={{ width: '100%' }} />
                      : g.zh_standard}
                  </td>
                  <td style={{ padding: '8px 10px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                    {editingKey === g.en ? (
                      <>
                        <a style={{ color: 'var(--accent-primary)' }} onClick={() => handleSaveEdit(g.en)}>保存</a>
                        <span style={{ color: 'var(--text-tertiary)', margin: '0 6px' }}>|</span>
                        <a style={{ color: 'var(--text-tertiary)' }} onClick={() => { setEditingKey(null); setEditingZh('') }}>取消</a>
                      </>
                    ) : (
                      <>
                        <a style={{ color: 'var(--accent-primary)' }}
                           onClick={() => { setEditingKey(g.en); setEditingZh(g.zh_standard) }}>修改</a>
                        <span style={{ color: 'var(--text-tertiary)', margin: '0 6px' }}>|</span>
                        <a style={{ color: '#dc2626' }} onClick={() => handleDeleteEntry(g.en)}>删除</a>
                      </>
                    )}
                  </td>
                </tr>
              ))}
              {/* 新增行 (列表底部, 用户视觉规范) */}
              {addingRow && (
                <tr style={{ borderBottom: '1px solid var(--border-subtle, #f0f0f0)', background: 'var(--bg-sunken, #fafafa)' }}>
                  <td style={{ padding: '8px 10px' }}>
                    <Input size="small" placeholder="如 spot" value={newEntryEn}
                           onChange={e => setNewEntryEn(e.target.value)} style={{ width: '100%' }} />
                  </td>
                  <td style={{ padding: '8px 10px' }}>
                    <Input size="small" placeholder="如 现货" value={newEntryZh}
                           onChange={e => setNewEntryZh(e.target.value)} style={{ width: '100%' }} />
                  </td>
                  <td style={{ padding: '8px 10px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <a style={{ color: 'var(--accent-primary)' }} onClick={handleAddEntry}>保存</a>
                    <span style={{ color: 'var(--text-tertiary)', margin: '0 6px' }}>|</span>
                    <a style={{ color: 'var(--text-tertiary)' }} onClick={() => setAddingRow(false)}>取消</a>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Spin>
      </Modal>
    </div>
  )
}

const TranslationView: React.FC<{ detail: ReportDetail }> = ({ detail }) => {
  return (
    <div>
      <Tabs items={[
        {
          key: 'file1',
          label: detail.file_name,
          children: <SourcePanel reportId={detail.id} fileName={detail.file_name} source="file1"
            label={detail.file_name} />,
        },
      ]} />
    </div>
  )
}

export default TranslationView

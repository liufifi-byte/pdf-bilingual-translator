import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

export interface ReportListItem {
  id: string
  file_name: string
  upload_time: string
  status: string
  report_type: string
  error: string
}

export interface TranslationResult {
  source: string
  target: string
  matched_terms: { en: string; zh_standard: string; category: string }[]
  tm_source: string
  comet_score: number | null
  needs_review: boolean
  page?: number | null
  block_id?: string | null
  block_type?: 'text' | 'table'
  translated_table?: { headers: string[]; rows: string[][] } | null
}

export interface ReportDetail {
  id: string
  file_name: string
  upload_time: string
  status: string
  report_type: string
  translations: TranslationResult[]
  error: string
}

export const listReports = () => api.get<ReportListItem[]>('/reports').then(r => r.data)

export const getReport = (id: string) =>
  api.get<ReportDetail>(`/reports/${id}`).then(r => r.data)

export const uploadReport = (file: File) => {
  const form = new FormData()
  form.append('file', file)
  return api.post<{ id: string; status: string; file_name: string; error: string }>('/upload', form)
    .then(r => r.data)
}

export interface ProgressInfo {
  id: string
  status: string
  error: string
  phase: string
  current: number
  total: number
  msg: string
}

export const getProgress = (id: string) =>
  api.get<ProgressInfo>(`/reports/${id}/status`).then(r => r.data)

// ─── 按需翻译接口 ───
export const pdfFileUrl = (fileName: string) => `/files/${fileName}`

export interface PageTranslation {
  page: number
  results: TranslationResult[]
  source: 'cache' | 'fresh' | 'empty-page' | 'in-progress'
}

// ─── 流式翻译视图 (原文画布 + DOM 浮层) ───
export interface OverlayBlockMeta {
  block_id: string
  bbox: [number, number, number, number]   // PDF 坐标 x0,y0,x1,y1
  font_size: number
  color: number                            // sRGB 整数
  bold: boolean
  italic: boolean
  len: number                              // 原文段字符数 (标题判定)
  lines: number                            // 原文段行数 (多行合并块不放大)
}
export interface PageStreamTranslation {
  page: number
  done: boolean
  blocks: OverlayBlockMeta[]               // 按揭示顺序 (阅读顺序)
  body_size: number | null                 // 正文主导字号 (标题放大/正文归一基准)
  translated: Record<string, string>       // block_id → 译文 (已完成)
}
/** 流式视图数据源: 轮询拉当前页浮层元数据 + 已完成译文 + 完成标志 */
export const getPageTranslationStream = (id: string, pageNum: number, source: string = 'file1',
                                         srcLang: string = 'auto', tgtLang: string = '中文') =>
  api.get<PageStreamTranslation>(`/reports/${id}/page/${pageNum}/translation`,
    { params: { source, src_lang: srcLang, tgt_lang: tgtLang } })
    .then(r => r.data)

/** 触发翻译某页 (前台按需调用) */
export const requestPageTranslation = (id: string, pageNum: number, source: string = 'file1',
                                       force?: boolean, srcLang: string = 'auto', tgtLang: string = '中文') =>
  api.post<PageTranslation>(`/reports/${id}/page/${pageNum}/translate`, null,
    { params: { source, ...(force ? { force: true } : {}),
                src_lang: srcLang, tgt_lang: tgtLang } })
    .then(r => r.data)

/** 手动启动翻译 (srcLang=auto → 后端自动检测源语言) */
export const startTranslation = (id: string, source: string = 'file1',
                                 srcLang: string = 'auto', tgtLang: string = '中文') =>
  api.post<{ id: string; background: string; source: string; total_pages: number }>(
    `/reports/${id}/start-translation`, null,
    { params: { source, src_lang: srcLang, tgt_lang: tgtLang } }
  ).then(r => r.data)

/** 源文档语言自动检测结果 (顶部「自动检测」框展示用) */
export const detectSourceLanguage = (id: string, source: string = 'file1') =>
  api.get<{ id: string; source: string; src_lang: string }>(
    `/reports/${id}/detect-language`, { params: { source } }).then(r => r.data)

/** 源文档 AI 概括 (refresh=true 强制重新生成, 默认命中后端缓存秒回) */
export const getAiSummary = (id: string, source: string = 'file1', refresh: boolean = false) =>
  api.post<{ summary: string; cached: boolean; src_lang?: string }>(
    `/reports/${id}/ai-summary`, null, { params: { source, refresh } }).then(r => r.data)

export interface TranslationProgressInfo {
  id: string
  done: number
  total: number
  translated_pages: number[]
  bilingual_pdf_ready: boolean
  phase: string
  msg: string
}

export const getTranslationProgress = (id: string, source: string = 'file1') =>
  api.get<TranslationProgressInfo>(`/reports/${id}/translation-progress`, {
    params: { source },
  }).then(r => r.data)

// ─── 全量翻译 + 译文 PDF 下载接口 ───

/** 触发全文翻译 (用户点「下载译文」按钮调; 换语言后旧 PDF 后端自动作废重生成) */
export const startFullTranslation = (id: string, source: string = 'file1',
                                     srcLang: string = 'auto', tgtLang: string = '中文') =>
  api.post<{ id: string; source: string; state: 'ready' | 'translating' | 'started'; current?: number; total?: number; total_pages?: number; msg?: string }>(
    `/reports/${id}/start-full-translation`, null,
    { params: { source, src_lang: srcLang, tgt_lang: tgtLang } }
  ).then(r => r.data)

export interface FullTranslationProgress {
  id: string
  source: string
  state: 'ready' | 'translating' | 'rendering' | 'done' | 'failed' | 'idle'
  current: number
  total: number
  msg: string
  bilingual_pdf_ready: boolean
}

/** 查询全文翻译进度 (轮询) */
export const getFullTranslationProgress = (id: string, source: string = 'file1') =>
  api.get<FullTranslationProgress>(`/reports/${id}/full-translation-progress`, { params: { source } })
    .then(r => r.data)

/** 译文 PDF 下载链接 */
export const bilingualPdfDownloadUrl = (id: string, source: string = 'file1') =>
  `/api/reports/${id}/bilingual-pdf?source=${source}`

// ─── 翻译术语库接口 ───

export interface GlossaryEntry {
  en: string
  zh_standard: string
}

/** 列出全部术语 */
export const listGlossary = () =>
  api.get<GlossaryEntry[]>('/glossary').then(r => r.data)

/** 新增术语 (重复 en → 后端返 409) */
export const addGlossary = (en: string, zh_standard: string) =>
  api.post<GlossaryEntry>('/glossary', { en, zh_standard }).then(r => r.data)

/** 修改术语译法 (按 en 大小写不敏感定位) */
export const updateGlossary = (en: string, zh_standard: string) =>
  api.put<GlossaryEntry>(`/glossary/${encodeURIComponent(en)}`, { en, zh_standard }).then(r => r.data)

/** 删除术语 */
export const deleteGlossary = (en: string) =>
  api.delete<{ ok: boolean; en: string }>(`/glossary/${encodeURIComponent(en)}`).then(r => r.data)

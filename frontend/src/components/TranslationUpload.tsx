import React, { useState } from 'react'
import { Upload, Button, Typography, message } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { uploadReport } from '../api'

const { Title, Text } = Typography

interface Props {
  onUploaded: (reportId: string) => void
}

/** 上传页 — 单份 PDF 文档。 */
const TranslationUpload: React.FC<Props> = ({ onUploaded }) => {
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)

  const startTranslate = async () => {
    if (!file) { message.error('请先选择 PDF 文件'); return }
    setUploading(true)
    try {
      const res = await uploadReport(file)
      message.success('已上传，可在下方查看译文')
      onUploaded(res.id)
    } catch (e: any) {
      message.error(`上传失败: ${e?.message}`)
    } finally {
      setUploading(false)
    }
  }

  return (
    <div style={{ maxWidth: 600, margin: '40px auto', padding: 32 }} className="u-card">
      <Title level={4} style={{ marginBottom: 8 }}>上传 PDF 文档，原文译文原位对照</Title>
      <Text type="secondary" style={{ display: 'block', marginBottom: 24 }}>
        上传后进入左右对照翻译阅读器：滑到哪页译哪页，支持术语库、
        多目标语言（中/英/韩/日/西/俄）与一键下载完整译文 PDF。
      </Text>

      <Upload.Dragger
        accept=".pdf"
        showUploadList={false}
        beforeUpload={(f) => { setFile(f); message.success(`已选择 ${f.name}`); return false }}
        style={{
          padding: 24, marginBottom: 24,
          borderColor: file ? 'var(--data-up)' : 'var(--border-default)',
          background: file ? 'var(--data-up-bg)' : 'var(--bg-elevated)',
        }}
      >
        <p style={{ margin: 0 }}>
          <UploadOutlined style={{ fontSize: 28, color: 'var(--accent-primary)' }} />
        </p>
        <p style={{ marginTop: 12, marginBottom: 4, fontSize: 14 }}>
          {file ? file.name : '点击或拖拽 PDF 到此区域'}
        </p>
        <p className="text-caption">仅支持 .pdf 文件</p>
      </Upload.Dragger>

      <Button type="primary" size="large" block onClick={startTranslate} loading={uploading}
        disabled={!file}>
        开始翻译
      </Button>
    </div>
  )
}

export default TranslationUpload

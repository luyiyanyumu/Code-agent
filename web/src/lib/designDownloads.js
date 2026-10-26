export function downloadDesignText({ filename, text, mime = 'text/plain' }, environment = globalThis) {
  const { document, URL, Blob, setTimeout } = environment
  if (!document?.body || !URL?.createObjectURL || !URL?.revokeObjectURL || !Blob || !setTimeout) throw new Error('当前浏览器无法导出文件。')
  const objectUrl = URL.createObjectURL(new Blob([String(text)], { type: `${mime};charset=utf-8` }))
  const anchor = document.createElement('a')
  try {
    anchor.href = objectUrl; anchor.download = filename; anchor.hidden = true
    document.body.appendChild(anchor); anchor.click()
  } finally {
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
  }
}

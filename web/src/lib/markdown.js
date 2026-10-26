import { Marked } from 'marked'

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character])
}

function safeHref(value) {
  const href = String(value || '').trim()
  if (/[\u0000-\u0020\u007f]/.test(href)) return ''
  if (!/^(?:https?:\/\/|mailto:)/i.test(href)) return ''
  try {
    const url = new URL(href)
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? href : ''
  } catch { return '' }
}

function linkHtml(href, title, label) {
  const safe = safeHref(href)
  if (!safe) return label
  const tooltip = title ? ` title="${escapeHtml(title)}"` : ''
  return `<a href="${escapeHtml(safe)}"${tooltip} target="_blank" rel="noopener noreferrer">${label}</a>`
}

// A private renderer avoids changing ChatView's marked instance. Model output
// may contain arbitrary HTML: only Markdown-generated elements enter the DOM.
// Images stay as links, so replaying a response cannot load remote resources.
const markdown = new Marked({
  gfm: true,
  breaks: true,
  async: false,
  renderer: {
    html({ text }) { return escapeHtml(text) },
    link({ href, title, tokens }) {
      return linkHtml(href, title, this.parser.parseInline(tokens))
    },
    image({ href, title, text }) {
      return linkHtml(href, title, escapeHtml(text || '查看图片'))
    },
  },
})

export function renderMarkdown(value) {
  return markdown.parse(String(value ?? ''))
}

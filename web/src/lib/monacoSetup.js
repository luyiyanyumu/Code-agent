// Monaco 装配（M18 · P2）：**worker 全部本地打包，不引 CDN**。
//
// 内网部署下 CDN 不可达，所以走 Vite 的 `?worker` 导入：worker 会被打包进
// dist，离线也能补全/校验 JSON、TS、CSS。
//
// 导入路径必须写成 `monaco-editor/<相对 esm/vs 的路径>`：
// monaco 0.56 的 package.json `exports` 只映射 `"./*": "./esm/vs/*.js"`，
// 写成 `monaco-editor/esm/vs/...` 会被拼成 `esm/vs/esm/vs/...` 而解析失败
// （真机踩过：构建期 Rolldown 直接 unresolved，dev 期整页 404）。
//
// 模型优先使用后端下发的绝对文件 URI；缺少 URI 时按项目/工作树隔离相对路径。
import * as monaco from './monacoRuntime.js'
import { conf as htmlConfiguration, language as htmlLanguage } from 'monaco-editor/languages/definitions/html/html'
import { editorTheme, currentEditorTheme, followEditorTheme } from './editorTheme.js'
import { vueLanguage } from './vueLanguage.js'
import { installTokenHoverProvider } from './tokenHover.js'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
import JsonWorker from 'monaco-editor/language/json/json.worker?worker'
import CssWorker from 'monaco-editor/language/css/css.worker?worker'
import HtmlWorker from 'monaco-editor/language/html/html.worker?worker'
import TsWorker from 'monaco-editor/language/typescript/ts.worker?worker'

let configured = false

export function ensureMonaco() {
  if (configured) return monaco
  configured = true
  self.MonacoEnvironment = {
    getWorker(_moduleId, label) {
      switch (label) {
        case 'json':
          return new JsonWorker()
        case 'css':
        case 'scss':
        case 'less':
          return new CssWorker()
        case 'html':
        case 'handlebars':
        case 'razor':
          return new HtmlWorker()
        case 'typescript':
        case 'javascript':
          return new TsWorker()
        default:
          return new EditorWorker()
      }
    },
  }
  monaco.editor.defineTheme('code-agent-light', editorTheme('light'))
  monaco.editor.defineTheme('code-agent-dark', editorTheme('dark'))
  monaco.languages.register({ id: 'vue', extensions: ['.vue'], aliases: ['Vue'] })
  monaco.languages.setLanguageConfiguration('vue', htmlConfiguration)
  monaco.languages.setMonarchTokensProvider('vue', vueLanguage(htmlLanguage))
  installTokenHoverProvider(monaco)
  return monaco
}

/** 语言 id 映射：后端给的是自己的语言名，这里翻成 Monaco 的 language id。 */
const LANG_MAP = {
  python: 'python',
  javascript: 'javascript',
  typescript: 'typescript',
  vue: 'vue',
  json: 'json',
  html: 'html',
  css: 'css',
  scss: 'scss',
  less: 'less',
  markdown: 'markdown',
  yaml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  shell: 'shell',
  powershell: 'powershell',
  bat: 'bat',
  sql: 'sql',
  xml: 'xml',
  go: 'go',
  rust: 'rust',
  java: 'java',
  c: 'c',
  cpp: 'cpp',
  csharp: 'csharp',
  php: 'php',
  ruby: 'ruby',
  kotlin: 'kotlin',
  swift: 'swift',
  dart: 'dart',
  lua: 'lua',
  dockerfile: 'dockerfile',
  terraform: 'hcl',
  svelte: 'html',
  plaintext: 'plaintext',
}

export function monacoLanguage(lang) {
  return LANG_MAP[lang] || 'plaintext'
}

export function modelUri(relPath, fileUri = '', scope = 'project') {
  if (fileUri) return monaco.Uri.parse(fileUri)
  return monaco.Uri.from({ scheme: 'code-agent', authority: 'project', path: `/${scope}/${relPath}` })
}

/** 取得（或创建）某个文件的模型；重复打开同一文件复用模型，保留撤销栈。 */
export function getModel(relPath, text, lang, fileUri = '', scope = 'project') {
  const uri = modelUri(relPath, fileUri, scope)
  const existing = monaco.editor.getModel(uri)
  if (existing) return existing
  return monaco.editor.createModel(text, monacoLanguage(lang), uri)
}

export function disposeModel(relPath, fileUri = '', scope = 'project') {
  const model = monaco.editor.getModel(modelUri(relPath, fileUri, scope))
  if (model) model.dispose()
}

export default monaco
export { currentEditorTheme, followEditorTheme }

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Isolated Monaco page: no app state, user drafts, backend or language processes.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/editor-highlighting-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = []
const samples = {
  java: `package demo;
import java.util.List;

/** 清晰区分类型、方法、参数与常量。 */
@Service
public class WelcomeService {
    private static final int MAX_RETRIES = 3;
    private final String prefix = "Hello";

    public String greet(String name) {
        if (name == null) {
            return prefix;
        }
        return prefix + ", " + name.trim();
    }
}`,
  python: `# 使用内置语法高亮，无需等待语言服务器
from dataclasses import dataclass

@dataclass
class Project:
    title: str
    retries: int = 3

def greet(name: str) -> str:
    if not name:
        return "Hello"
    return f"Hello, {name}"
`,
  javascript: `// 模块、字符串与控制流分别着色
import { computed } from 'vue';
const count = 3;
export function greet(name) {
  if (!name) return 'Hello';
  return 'Hello, ' + name;
}
`,
  typescript: `// TypeScript 类型与运行代码同时可读
interface Project {
  title: string;
  retries: number;
}
export function greet(project: Project): string {
  return 'Hello, ' + project.title;
}
const options: Project = { title: 'Workspace', retries: 3 };
`,
  vue: `<template>
  <button v-if="ready" @click.stop="greet">{{ title + ' · ' + count }}</button>
</template>
<script setup lang="ts">
import { ref } from 'vue'
const title: string = 'Workspace'
const count = ref(3)
const ready = true
function greet(): void { count.value += 1 }
</script>
<style scoped lang="scss">
$accent: #355dfb;
button { color: $accent; &:hover { opacity: 0.85; } }
</style>`,
}
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
body { margin:0; background:#f4f6f9; color:#263244; font:14px system-ui; }
header { padding:18px 26px; border-bottom:1px solid #e3e7ef; background:white; display:flex; gap:12px; align-items:center; }
header strong { margin-right:20px; } button { border:1px solid #d6deeb; background:white; color:#455571; border-radius:6px; padding:7px 16px; cursor:pointer; }
main { margin:24px; display:grid; grid-template-columns:200px 1fr; min-height:620px; border:1px solid #dce3ed; border-radius:10px; overflow:hidden; }
aside { padding:20px; background:#f7f9fc; border-right:1px solid #e3e7ef; } aside div { padding:8px 0; } #editor { height:620px; }
</style></head><body><header><strong>IDE 代码高亮</strong><button data-lang="java">Java</button><button data-lang="python">Python</button><button data-lang="javascript">JavaScript</button><button data-lang="typescript">TypeScript</button><button data-lang="vue">Vue</button><button id="dark">深色</button></header><main><aside id="legend"></aside><div id="editor"></div></main>
<script type="module">
import { ensureMonaco, getModel, currentEditorTheme, followEditorTheme } from '/src/lib/monacoSetup.js';
import { editorTheme } from '/src/lib/editorTheme.js';
import { LspClient } from '/src/lib/lspClient.js';
const samples = ${JSON.stringify(samples).replaceAll('<', String.fromCharCode(92) + 'u003c')};
const monaco = ensureMonaco();
const models = Object.fromEntries(Object.entries(samples).map(([language,text]) => [language,getModel('sample.'+language,text,language,'file:///E:/highlight-fixture/sample.'+language)]));
const editor = monaco.editor.create(document.querySelector('#editor'),{model:models.java,theme:currentEditorTheme(),'semanticHighlighting.enabled':true,fontSize:16,lineHeight:28,minimap:{enabled:false},automaticLayout:true,scrollBeyondLastLine:false,bracketPairColorization:{enabled:true},guides:{bracketPairs:'active'}});
followEditorTheme(monaco);
for (const button of document.querySelectorAll('[data-lang]')) button.onclick=()=>editor.setModel(models[button.dataset.lang]);
document.querySelector('#dark').onclick=()=>document.documentElement.setAttribute('data-theme',document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');
const theme=editorTheme();
for (const [token,label] of [['keyword','关键字 / 控制流'],['class','类型 / 类'],['method','方法 / 函数'],['parameter','参数'],['property','字段'],['string','字符串'],['number','数值'],['decorator','注解'],['comment','注释']]) {
  const div=document.createElement('div');div.textContent=label;div.style.color='#'+theme.rules.find(rule=>rule.token===token).foreground;document.querySelector('#legend').append(div);
}
const data=[];let lastLine=0,lastStart=0;
for (const [line,text] of samples.java.split(String.fromCharCode(10)).entries()) for (const match of text.matchAll(/[A-Za-z_][A-Za-z_0-9]*/g)) {
  const type=['WelcomeService','greet','name','prefix'].indexOf(match[0]);if(type<0)continue;const start=match.index;
  data.push(line-lastLine,line===lastLine?start-lastStart:start,match[0].length,type,0);lastLine=line;lastStart=start;
}
const frames=[];
class FixtureSocket { static OPEN=1; readyState=1;
  constructor(){queueMicrotask(()=>this.onmessage?.({data:JSON.stringify({method:'code-agent/ready',params:{server:'Fixture Java',capabilities:{codeActionProvider:true,referencesProvider:true,semanticTokensProvider:{legend:{tokenTypes:['class','method','parameter','property'],tokenModifiers:[]},full:true}}}})}))}
  send(raw){const frame=JSON.parse(raw);frames.push(frame);if(frame.method==='textDocument/semanticTokens/full')queueMicrotask(()=>this.onmessage?.({data:JSON.stringify({id:frame.id,result:{data}})}));else if(frame.id!==undefined)queueMicrotask(()=>this.onmessage?.({data:JSON.stringify({id:frame.id,result:[]})}))}
  close(){this.readyState=3;this.onclose?.()}
}
window.WebSocket=FixtureSocket;
const client=new LspClient({projectId:'fixture',lang:'java',autoReconnect:false});client.connect();client.openDocument({uri:models.java.uri.toString(),model:models.java});client.registerProviders();
window.__highlight={monaco,models,editor,client,frames,samples};
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let failure
try {
  const context = await browser.newContext({ viewport: { width: 1420, height: 820 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return route.abort() }
    if (url.pathname === '/__editor-highlighting-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__editor-highlighting-fixture`, { waitUntil: 'networkidle' })
  await page.waitForFunction(() => !!window.__highlight?.editor && window.__highlight.frames.some(frame => frame.method === 'textDocument/semanticTokens/full'))
  await page.waitForFunction(() => [...document.querySelectorAll('.view-line span')].some(span => span.textContent === 'WelcomeService' && getComputedStyle(span).color === 'rgb(8, 127, 140)'))
  checks.push('Real LSP full semantic tokens colour Java classes')
  const colours = await page.locator('.view-lines').evaluate(element => [...new Set([...element.querySelectorAll('span')].filter(span => span.textContent.trim()).map(span => getComputedStyle(span).color))])
  assert.ok(colours.length >= 8, JSON.stringify(colours)); checks.push('Java has at least eight legible syntax/semantic colours')
  for (const language of Object.keys(samples)) {
    await page.locator(`[data-lang="${language}"]`).click()
    await page.waitForFunction(language => window.__highlight.monaco.editor.tokenize(window.__highlight.samples[language], language).flat().some(token => token.type.includes('keyword') || token.type.includes('tag')), language)
    if (language === 'vue') await page.waitForFunction(() => window.__highlight.monaco.editor.tokenize(window.__highlight.samples.vue, 'vue').flat().some(token => token.type.endsWith('.scss')))
    const types = await page.evaluate(language => [...new Set(window.__highlight.monaco.editor.tokenize(window.__highlight.samples[language], language).flat().map(token => token.type))], language)
    assert.ok(types.length >= 5, `${language}: ${types}`)
    if (language === 'vue') { assert.ok(types.some(type => type.endsWith('.ts'))); assert.ok(types.some(type => type.endsWith('.scss'))); assert.ok(types.some(type => type === 'attribute.name.directive.vue')); checks.push('Vue SFC embeds TypeScript and SCSS and highlights directives') }
    checks.push(`${language} lexical highlighting works independently of language-server startup`)
  }
  await page.locator('[data-lang="java"]').click()
  await page.screenshot({ path: path.join(output, 'ide-highlighting-light.png'), fullPage: true })
  await page.locator('#dark').click()
  await page.waitForFunction(() => document.querySelector('.monaco-editor')?.classList.contains('vs-dark'))
  await page.waitForFunction(() => [...document.querySelectorAll('.view-line span')].some(span => span.textContent === 'WelcomeService' && getComputedStyle(span).color === 'rgb(113, 200, 207)'))
  const background = await page.locator('.monaco-editor-background').first().evaluate(element => getComputedStyle(element).backgroundColor)
  assert.equal(background, 'rgb(27, 35, 48)'); checks.push('Application appearance changes syntax and semantic palette without recreating models')
  await page.screenshot({ path: path.join(output, 'ide-highlighting-dark.png'), fullPage: true })
  const actions = await page.evaluate(() => ['editor.action.rename','editor.action.formatDocument','actions.find'].map(name => [name,!!window.__highlight.editor.getAction(name)]))
  assert.ok(actions.every(([, present]) => present), JSON.stringify(actions))
  await page.evaluate(() => { const editor=window.__highlight.editor;editor.focus();editor.setPosition({lineNumber:10,column:20});editor.trigger('fixture','editor.action.quickFix',{});editor.trigger('fixture','editor.action.goToReferences',{}) })
  await page.waitForFunction(() => ['textDocument/codeAction','textDocument/references'].every(method => window.__highlight.frames.some(frame => frame.method===method)))
  checks.push('IDE navigation, fixes, rename, format and find retained by lightweight Monaco entry')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
} catch (error) { failure = error; errors.push(error.message) }
finally { await browser.close(); fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected }, null, 2)) }
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, report: path.join(output, 'report.json') }))

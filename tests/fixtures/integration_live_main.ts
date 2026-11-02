import { createApp, h, ref } from '/vendor/vue.js'

type Item = { id: number, title: string }
declare global { interface Window { __BACKEND_ORIGIN__: string } }

createApp({ setup() {
  const result = ref('等待操作'), items = ref<Item[]>([]), selected = ref(1)
  async function request(path: string | URL, options: RequestInit = {}) {
    const response = await fetch(path, options), data = await response.json()
    result.value = JSON.stringify({ status: response.status, ...data })
    return data
  }
  const json = (method: string, value: unknown) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
  async function read() { const data = await request('/api/items'); items.value = data.items }
  async function add() { const item = await request(window.__BACKEND_ORIGIN__ + '/api/items', json('POST', { title: '画布创建' })); selected.value = item.id }
  async function update() { await request(new URL(`/api/items/${selected.value}`, window.__BACKEND_ORIGIN__), json('PATCH', { title: '画布已更新' })) }
  async function remove() { await request(`/api/items/${selected.value}`, { method: 'DELETE' }) }
  async function login() {
    const data = await request('/api/login', json('POST', { username: 'fixture' }))
    await request('/api/private', { headers: { Authorization: `Bearer ${data.token}` }, credentials: 'include' })
  }
  function xhr() {
    const value = new XMLHttpRequest()
    value.open('GET', window.__BACKEND_ORIGIN__ + '/api/xhr')
    value.withCredentials = true
    value.onload = () => { result.value = JSON.stringify({ status: value.status, ...JSON.parse(value.responseText) }) }
    value.onerror = () => { result.value = 'XHR network failure' }
    value.send()
  }
  const action = (label: string, handler: () => unknown) => h('button', { onClick: async () => { try { await handler() } catch (error) { result.value = String(error) } } }, label)
  return () => h('main', [
    h('p', { class: 'eyebrow' }, '真实 Vue + Vite + Python 项目'),
    h('h1', '联调示例 · v1'), h('p', '从页面发起接口请求，在 IDE 中查看响应和双端日志。'),
    h('div', { class: 'actions' }, [action('读取任务', read), action('新增任务', add), action('修改任务', update), action('删除任务', remove), action('登录并验证', login), action('发送 XHR', xhr), action('触发后端错误', () => request('/api/error'))]),
    h('output', { 'data-testid': 'fixture-status' }, result.value),
    h('ul', items.value.map(item => h('li', { key: item.id }, `${item.id}. ${item.title}`))),
  ])
} }).mount('#app')

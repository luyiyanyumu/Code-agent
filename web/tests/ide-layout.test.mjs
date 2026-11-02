import test from 'node:test'
import assert from 'node:assert/strict'
import { readIdeLayout, persistIdeLayout, defaultIdeLayout, IDE_LAYOUT_KEY } from '../src/lib/ideLayout.js'
test('saved layout restores valid visibility, panel and bounded size without trusting corrupt values', () => {
  const storage = { getItem: () => JSON.stringify({ left: false, right: 'false', bottom: true, bottomTab: 'terminal', bottomHeight: 9000 }) }
  assert.deepEqual(readIdeLayout(storage), { left: false, right: true, bottom: true, bottomTab: 'terminal', bottomHeight: 600 })
  assert.deepEqual(readIdeLayout({getItem:()=>'{broken'}), defaultIdeLayout)
  assert.deepEqual(readIdeLayout(null), defaultIdeLayout)
})
test('layout persistence keeps the same scope and tolerates blocked storage', () => {
  const values = new Map(), storage = {getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value)}
  assert.equal(persistIdeLayout(storage, { ...defaultIdeLayout, left: false, bottom: true, bottomTab: 'debug', bottomHeight: 310 }), true)
  assert.equal(values.size, 1); assert.ok(values.has(IDE_LAYOUT_KEY))
  assert.equal(readIdeLayout(storage).bottomHeight, 310)
  assert.equal(readIdeLayout(storage).bottomTab, 'debug')
  assert.equal(persistIdeLayout({setItem:()=>{throw Error('blocked')}}, defaultIdeLayout), false)
})

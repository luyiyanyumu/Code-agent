import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import { DESIGN_VIEWPORT_PRESETS, normalizeDesignViewport } from '../src/lib/designViewport.js'
import { downloadDesignText } from '../src/lib/designDownloads.js'

test('device presets use supported logical viewports and malformed stored sizes never become CSS dimensions', () => {
  for (const preset of DESIGN_VIEWPORT_PRESETS) assert.deepEqual(normalizeDesignViewport(preset), { width: preset.width, height: preset.height })
  assert.deepEqual(normalizeDesignViewport({ width: 240, height: 2160 }), { width: 240, height: 2160 })
  for (const value of [null, {}, { width: '390', height: 844 }, { width: NaN, height: 900 }, { width: Infinity, height: 800 }, { width: 390.5, height: 844 }, { width: 239, height: 844 }, { width: 3841, height: 900 }, { width: 390, height: 2161 }]) assert.equal(normalizeDesignViewport(value), null)
})

test('production viewport popover preserves the current dimensions on open and only emits valid explicit changes', async t => {
  const source = await readFile(new URL('../src/components/code/DesignViewport.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source); assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'viewport-fixture' })
  const template = compileTemplate({ source: descriptor.template.content, filename: 'DesignViewport.vue', id: 'viewport-fixture', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const props = Vue.reactive({ width: 750, height: 379, custom: false, disabled: false }), events = []
  const scope = Vue.effectScope(); t.after(() => scope.stop())
  let mount, unmount
  const listeners = new Map()
  const bindings = { ref: Vue.ref, watch: Vue.watch, DESIGN_VIEWPORT_PRESETS, normalizeDesignViewport, defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    onMounted: fn => { mount = fn }, onBeforeUnmount: fn => { unmount = fn }, window: { addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: type => listeners.delete(type) } }
  const executable = descriptor.scriptSetup.content.replace(/^import\s+.*?from\s+['"][^'"]+['"]\s*$/gm, '')
  const state = scope.run(() => new Function(...Object.keys(bindings), `${executable}\nreturn {open,draftWidth,draftHeight,error,apply,close,rootEl,popoverStyle}`)(...Object.values(bindings)))
  mount(); t.after(() => unmount())
  state.open.value = true; await Vue.nextTick()
  assert.equal(state.draftWidth.value, 750); assert.equal(state.draftHeight.value, 379); assert.equal(events.length, 0)
  state.apply(1440, 900)
  assert.deepEqual(events, [['change', { width: 1440, height: 900 }]])
  assert.equal(state.open.value, false)
  state.open.value = true; await Vue.nextTick(); state.draftWidth.value = 'invalid'; state.apply()
  assert.match(state.error.value, /240/); assert.equal(state.open.value, true); assert.equal(events.length, 1)
  props.disabled = true; state.apply(390, 844); assert.equal(events.length, 1)
  let stopped = 0; state.close({ key: 'Escape', stopPropagation: () => stopped++ })
  assert.equal(state.open.value, false); assert.equal(stopped, 1)
  await Vue.nextTick()
  state.rootEl.value = { getBoundingClientRect: () => ({ left: 210, right: 246 }), closest: () => ({ getBoundingClientRect: () => ({ left: 0, right: 390 }) }) }
  state.open.value = true; await Vue.nextTick()
  const left = 210 + Number.parseFloat(state.popoverStyle.value.left), width = Number.parseFloat(state.popoverStyle.value.width)
  assert.ok(left >= 12 && left + width <= 378, 'the phone popover is clamped inside the actual designer container')
  listeners.get('resize')(); assert.equal(state.open.value, false, 'a resized window closes stale popup coordinates')
  unmount(); assert.equal(listeners.has('resize'), false)
})

test('text exports preserve Unicode and literal content, clean their temporary node, and revoke download URLs even on errors', async () => {
  const events = [], timers = []; let blob, fail = false
  const environment = { Blob, URL: { createObjectURL: value => { blob = value; return 'blob:fixture' }, revokeObjectURL: url => events.push(['revoke', url]) },
    document: { body: { appendChild: anchor => events.push(['append', anchor.download]) }, createElement: tag => { assert.equal(tag, 'a'); return { click() { events.push(['click', this.href, this.download]); if (fail) throw new Error('blocked') }, remove() { events.push(['remove']) } } } }, setTimeout: fn => timers.push(fn) }
  const text = '# 验收报告\n- 中文 <script>alert(1)</script>\n'
  downloadDesignText({ filename: 'design-report.md', text, mime: 'text/markdown' }, environment)
  assert.equal(await blob.text(), text); assert.equal(blob.type, 'text/markdown;charset=utf-8')
  assert.deepEqual(events.slice(0, 3), [['append', 'design-report.md'], ['click', 'blob:fixture', 'design-report.md'], ['remove']])
  timers.shift()(); assert.deepEqual(events.at(-1), ['revoke', 'blob:fixture'])
  fail = true
  assert.throws(() => downloadDesignText({ filename: 'styles.css', text: '/* managed */' }, environment), /blocked/)
  assert.deepEqual(events.at(-1), ['remove']); timers.shift()(); assert.deepEqual(events.at(-1), ['revoke', 'blob:fixture'])
  assert.throws(() => downloadDesignText({ filename: 'styles.css', text: '' }, {}), /无法导出/)
})

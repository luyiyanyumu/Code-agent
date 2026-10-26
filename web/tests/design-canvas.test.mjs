import assert from 'node:assert/strict'
import test from 'node:test'
import { clampZoom, stepZoom, wheelZoom, fitCanvasZoom, zoomScrollPosition, isDesignTypingTarget } from '../src/lib/designCanvas.js'

test('zoom normalization preserves fractional percentages and clamps finite boundaries', () => {
  for (const value of [undefined, NaN, Infinity, -Infinity, null, '', '150', {}]) assert.equal(clampZoom(value), 100)
  for (const value of [-100, 0, 24, 25]) assert.equal(clampZoom(value), 25)
  for (const value of [200, 201, Number.MAX_VALUE]) assert.equal(clampZoom(value), 200)
  assert.equal(clampZoom(87.5), 87.5)
})

test('zoom steps use 25 percentage points and stop at each limit', () => {
  assert.equal(stepZoom(100, 1), 125)
  assert.equal(stepZoom(100, -1), 75)
  assert.equal(stepZoom(87.5, 1), 112.5)
  assert.equal(stepZoom(190, 1), 200)
  assert.equal(stepZoom(30, -1), 25)
  assert.equal(stepZoom(200, 1), 200)
  assert.equal(stepZoom(25, -1), 25)
  assert.equal(stepZoom(NaN, 1), 125)
  for (const direction of [0, 2, -2, NaN, undefined, '1']) assert.equal(stepZoom(100, direction), 100)
})

test('wheel zoom preserves small continuous deltas and accumulates their magnitude rather than jumping by 25 points', () => {
  const zoom = wheelZoom(100, { deltaY: -0.5 })
  assert.ok(zoom > 100 && zoom < 101)
  assert.ok(Math.abs(zoom - 100 * Math.exp(0.001)) < 1e-10)
  assert.ok(wheelZoom(100, { deltaY: -2 }) > wheelZoom(100, { deltaY: -1 }))
  assert.ok(wheelZoom(100, { deltaY: 0.5 }) < 100)
  const accumulated = [-0.5, -1.25, -2.75].reduce((current, deltaY) => wheelZoom(current, { deltaY }), 100)
  assert.ok(Math.abs(accumulated - wheelZoom(100, { deltaY: -4.5 })) < 1e-10)
})

test('wheel units normalize pixel, line and page deltas to the same logical distance', () => {
  assert.equal(wheelZoom(100, { deltaY: -1, deltaMode: 1 }), wheelZoom(100, { deltaY: -16, deltaMode: 0 }))
  assert.equal(wheelZoom(100, { deltaY: -0.1, deltaMode: 2, pageHeight: 400 }), wheelZoom(100, { deltaY: -40 }))
  assert.equal(wheelZoom(100, { deltaY: 0.01, deltaMode: 2 }), wheelZoom(100, { deltaY: 8 }))
  assert.equal(wheelZoom(87.5, { deltaY: -2, deltaMode: 0 }), wheelZoom(87.5, { deltaY: -2 }))
})

test('wheel zoom rejects malformed input and clamps extreme finite deltas without resetting the zoom', () => {
  assert.equal(wheelZoom(87.5), 87.5)
  for (const deltaY of [0, undefined, null, NaN, Infinity, -Infinity, '-1', {}, []]) assert.equal(wheelZoom(87.5, { deltaY }), 87.5)
  for (const deltaMode of [-1, 3, 0.5, null, NaN, Infinity, '1']) assert.equal(wheelZoom(87.5, { deltaY: -1, deltaMode }), 87.5)
  assert.equal(wheelZoom(100, { deltaY: -Number.MAX_VALUE }), 200)
  assert.equal(wheelZoom(100, { deltaY: Number.MAX_VALUE }), 25)
  assert.equal(wheelZoom(200, { deltaY: -1 }), 200)
  assert.equal(wheelZoom(25, { deltaY: 1 }), 25)
})

test('fit zoom uses the limiting axis and reserves padding on both sides', () => {
  assert.equal(fitCanvasZoom({ viewportWidth: 1096, viewportHeight: 896, frameWidth: 1000, frameHeight: 400 }), 100)
  assert.equal(fitCanvasZoom({ viewportWidth: 1096, viewportHeight: 496, frameWidth: 400, frameHeight: 800 }), 50)
  assert.equal(fitCanvasZoom({ viewportWidth: 1000, viewportHeight: 600, frameWidth: 800, frameHeight: 800, padding: 0 }), 75)
  assert.equal(fitCanvasZoom({ viewportWidth: 600, viewportHeight: 400, frameWidth: 1600, frameHeight: 1000, padding: 0 }), 37.5)
  assert.equal(fitCanvasZoom({ viewportWidth: 4000, viewportHeight: 3000, frameWidth: 100, frameHeight: 100 }), 200)
  assert.equal(fitCanvasZoom({ viewportWidth: 20, viewportHeight: 20, frameWidth: 1000, frameHeight: 1000 }), 25)
})

test('fit zoom handles invalid dimensions and extreme padding without NaN or overflow', () => {
  assert.equal(fitCanvasZoom(), 100)
  const dimensions = { viewportWidth: 1000, viewportHeight: 800, frameWidth: 1000, frameHeight: 800 }
  for (const field of Object.keys(dimensions)) {
    for (const value of [0, -1, NaN, Infinity, undefined, '1000']) assert.equal(fitCanvasZoom({ ...dimensions, [field]: value }), 100)
  }
  assert.equal(fitCanvasZoom({ ...dimensions, padding: -10 }), 100)
  assert.equal(fitCanvasZoom({ ...dimensions, padding: NaN }), fitCanvasZoom(dimensions))
  assert.equal(fitCanvasZoom({ ...dimensions, padding: Number.MAX_VALUE }), 25)
  assert.equal(fitCanvasZoom({ viewportWidth: Number.MAX_VALUE, viewportHeight: Number.MAX_VALUE, frameWidth: Number.MIN_VALUE, frameHeight: Number.MIN_VALUE, padding: 0 }), 200)
})

test('pointer zoom preserves its content point when scroll bounds do not intervene', () => {
  const before = { scrollLeft: 320, scrollTop: 160, anchorX: 120, anchorY: 80, previousZoom: 100, nextZoom: 150 }
  const after = zoomScrollPosition(before)
  assert.deepEqual(after, { scrollLeft: 540, scrollTop: 280 })
  assert.equal((before.scrollLeft + before.anchorX) / before.previousZoom, (after.scrollLeft + before.anchorX) / before.nextZoom)
  assert.equal((before.scrollTop + before.anchorY) / before.previousZoom, (after.scrollTop + before.anchorY) / before.nextZoom)
  assert.deepEqual(zoomScrollPosition({ ...before, ...after, previousZoom: 150, nextZoom: 100 }), { scrollLeft: 320, scrollTop: 160 })
  assert.deepEqual(zoomScrollPosition({ ...before, nextZoom: 100 }), { scrollLeft: 320, scrollTop: 160 })
})

test('scroll zoom clamps at the origin and normalizes invalid inputs to finite positions', () => {
  assert.deepEqual(zoomScrollPosition({ scrollLeft: 10, scrollTop: 20, anchorX: 100, anchorY: 200, previousZoom: 100, nextZoom: 25 }), { scrollLeft: 0, scrollTop: 0 })
  assert.deepEqual(zoomScrollPosition(), { scrollLeft: 0, scrollTop: 0 })
  assert.deepEqual(zoomScrollPosition({ scrollLeft: NaN, scrollTop: -20, anchorX: Infinity, anchorY: -10, previousZoom: NaN, nextZoom: Infinity }), { scrollLeft: 0, scrollTop: 0 })
  assert.deepEqual(zoomScrollPosition({ scrollLeft: 20, scrollTop: 10, previousZoom: 0, nextZoom: 100 }), { scrollLeft: 80, scrollTop: 40 })
  assert.deepEqual(zoomScrollPosition({ scrollLeft: Number.MAX_VALUE, scrollTop: Number.MAX_VALUE, anchorX: Number.MAX_VALUE, anchorY: Number.MAX_VALUE, previousZoom: -1, nextZoom: 200 }), { scrollLeft: Number.MAX_SAFE_INTEGER, scrollTop: Number.MAX_SAFE_INTEGER })
})

function element(tagName, parentElement = null, editable = null) {
  return { tagName, parentElement, getAttribute: name => name === 'contenteditable' ? editable : null }
}

test('typing protection recognizes native controls and editable ancestors including text nodes', () => {
  for (const tag of ['INPUT', 'textarea', 'Select']) {
    assert.equal(isDesignTypingTarget(element(tag)), true)
    assert.equal(isDesignTypingTarget(element('option', element(tag))), true)
  }
  for (const attribute of ['', 'true', 'TRUE', 'plaintext-only']) {
    const editable = element('div', null, attribute)
    assert.equal(isDesignTypingTarget(element('span', editable)), true)
    assert.equal(isDesignTypingTarget({ parentNode: editable }), true)
  }
  assert.equal(isDesignTypingTarget({ isContentEditable: true }), true)
  assert.equal(isDesignTypingTarget({ parentNode: { host: element('input') } }), true)
})

test('ordinary canvas targets and malformed ancestry do not trigger typing protection', () => {
  for (const target of [null, undefined, 'input', element('canvas'), element('button'), element('div', null, 'false'), element('div', null, 'inherit')]) assert.equal(isDesignTypingTarget(target), false)
  const cycle = element('div')
  cycle.parentElement = cycle
  assert.equal(isDesignTypingTarget(cycle), false)
})

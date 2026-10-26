import assert from 'node:assert/strict'
import test from 'node:test'
import { useNavigation } from '../src/composables/useNavigation.js'

test('history restores cursor columns, truncates forward branch and separates projects', () => {
  const nav = useNavigation()
  nav.activate('a')
  nav.visit({ path: 'a.py', line: 1 })
  nav.visit({ path: 'b.py', line: 7 }, { path: 'a.py', line: 9, column: 4 })
  const ticket = nav.prepare(-1, { path: 'b.py', line: 12, column: 3 })
  assert.deepEqual(ticket.point, { path: 'a.py', line: 9, column: 4 })
  assert.equal(nav.commit(ticket), true)
  assert.equal(nav.canForward.value, true)
  nav.visit({ path: 'c.py', line: 2 })
  assert.equal(nav.canForward.value, false)
  nav.activate('b')
  assert.equal(nav.canBack.value, false)
  nav.visit({ path: 'b.txt' })
  nav.activate('a')
  assert.equal(nav.state.value.entries.at(-1).path, 'c.py')
})
test('stale navigation completion cannot move history after another explicit jump', () => {
  const nav = useNavigation(3)
  nav.activate('p')
  for (let i = 0; i < 5; i++) nav.visit({ path: `${i}.py` })
  assert.equal(nav.state.value.entries.length, 3)
  const ticket = nav.prepare(-1)
  nav.visit({ path: 'new.py' })
  assert.equal(nav.commit(ticket), false)
  assert.equal(nav.state.value.entries.at(-1).path, 'new.py')
})

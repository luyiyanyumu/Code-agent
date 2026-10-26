import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { parse } from '@vue/compiler-sfc'

test('legacy team panel forwards only to background tasks and cannot restore a worker composer selector', async () => {
  const source = await readFile(new URL('../src/components/code/AgentTeamPanel.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  assert.match(descriptor.scriptSetup.content, /import BackgroundAgentTasks from '.\/BackgroundAgentTasks.vue'/)
  assert.match(descriptor.template.content, /<BackgroundAgentTasks v-bind="\$attrs"/)
  assert.doesNotMatch(source, /选择项目智能体|回 Lead|team-menu|emit\('select'/)
})

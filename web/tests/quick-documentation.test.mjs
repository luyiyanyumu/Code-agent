import assert from 'node:assert/strict'
import test from 'node:test'
import { buildQuickDocumentation, documentationHref } from '../src/lib/quickDocumentation.js'

test('Java annotation owner and declaration are derived from real signatures with all Chinese sections intact', () => {
  const sections = [
    { kind: 'markdown', value: '**作用：** 根据请求路径查找控制器。\n\n**返回类型：** `void`\n\n识别依据：语言服务语义信息。' },
    { kind: 'markdown', value: '**业务信息：** 管理用户接口。\n\n**文档中文翻译：** 已翻译。' },
    { kind: 'code', language: 'java', value: 'org.example.web.annotation\n@Target({ElementType.TYPE, ElementType.METHOD})\n@Retention(RetentionPolicy.RUNTIME)\npublic @interface RouteMapping\nextends java.lang.annotation.Annotation' },
    { kind: 'markdown', value: '完整说明\n' + '更多中文内容。'.repeat(10000) + '\n末尾信息' },
    { kind: 'plaintext', value: '**纯文本**\n@param value 参数' },
  ]
  const original = structuredClone(sections)
  const result = buildQuickDocumentation({ sections })
  assert.equal(result.owner, 'org.example.web.annotation')
  assert.equal(result.declaration, sections[2].value)
  assert.equal(result.language, 'java')
  assert.deepEqual(result.sections.map(row => ({ kind: row.kind, value: row.value })), sections.map(row => ({ kind: row.kind, value: row.value })))
  assert.deepEqual(sections, original)
  assert.equal(result.sections.filter(row => row.declaration).length, 1)
})

test('fenced Python and TypeScript declarations work without language-specific project assumptions', () => {
  for (const [language, declaration] of [['python', 'def find_user(name: str) -> User:'], ['typescript', 'export function findUser(name: string): Promise<User>']]) {
    const value = `\`\`\`${language}\n${declaration}\n\`\`\`\n\n根据名称查找用户。`
    const result = buildQuickDocumentation({ sections: [{ kind: 'markdown', value }] })
    assert.equal(result.declaration, declaration)
    assert.equal(result.language, language)
    assert.equal(result.sections[0].value, value)
    assert.equal(result.owner, '')
  }
})

test('reference links exclude commands, files, credential URLs, examples and plaintext', () => {
  const sections = [
    { kind: 'markdown', value: '[参考](https://example.test/docs)\n\n[再次](https://example.test/docs)\n\n[运行](command:run)\n\n[危险](javascript:alert(1))\n\n[文件](file:///secret)\n\n[伪装](https://user:pass@example.test/)' },
    { kind: 'code', language: 'md', value: '[示例](https://example.test/example)' },
    { kind: 'plaintext', value: '[原文](https://example.test/plain)' },
    { kind: 'markdown', value: '```md\n[示例](https://example.test/code)\n```\n\n![架构](https://example.test/image.png)' },
  ]
  assert.deepEqual(buildQuickDocumentation({ sections }).links, [{ href: 'https://example.test/docs', label: '参考' }])
})

test('usage examples do not become declarations and arbitrary plaintext cannot become Markdown metadata', () => {
  const result = buildQuickDocumentation({ sections: [
    { kind: 'plaintext', value: 'package org.fake;\npublic class Fake {}\n[文档](https://example.test/fake)' },
    { kind: 'markdown', value: '用法示例：\n\n```java\npublic class Demo {}\n```' },
  ] })
  assert.equal(result.owner, '')
  assert.equal(result.declaration, '')
  assert.deepEqual(result.links, [])
  assert.equal(result.sections.length, 2)
})

test('explicit package and qualified method owners are not inferred from unrelated return types', () => {
  assert.equal(buildQuickDocumentation({ sections: [{ kind: 'code', language: 'java', value: 'package org.example;\npublic class Service {}' }] }).owner, 'org.example')
  assert.equal(buildQuickDocumentation({ sections: [{ kind: 'code', language: 'java', value: 'public static String java.util.Objects.toString(Object value)' }] }).owner, 'java.util.Objects')
  assert.equal(buildQuickDocumentation({ sections: [{ kind: 'code', language: 'java', value: 'public java.util.List<String> findUsers(String name)' }] }).owner, '')
})

test('missing malformed sections and invalid reference targets remain inert', () => {
  for (const value of [null, undefined, {}, { sections: [null, { kind: 'html', value: '<script>attack()</script>' }] }]) {
    assert.deepEqual(buildQuickDocumentation(value), { declaration: '', language: 'plaintext', owner: '', links: [], sections: [] })
  }
  for (const href of ['javascript:run()', 'command:run', 'file:///C:/a', '//example.test', 'https://example.test/ bad', 'https://example.test/\\evil', 'https://name@example.test', 'data:text/html,attack', 'https://']) assert.equal(documentationHref(href), '')
  assert.equal(documentationHref('https://example.test/docs#方法'), 'https://example.test/docs#%E6%96%B9%E6%B3%95')
})

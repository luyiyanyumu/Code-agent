import { cpSync, mkdirSync } from 'node:fs'
mkdirSync('dist/licenses', { recursive: true })
for (const file of ['LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md']) cpSync(file, `dist/licenses/${file}`)
cpSync('licenses', 'dist/licenses/dependencies', { recursive: true })

// Build-time verification uses the build host's Node, never a staged process.
import {readFile} from 'node:fs/promises'
import {resolve,join} from 'node:path'
import {loadLanguageTools} from '../npm/language-runtime.mjs'
const root=resolve(process.argv[2])
const manifest=JSON.parse((await readFile(join(root,'desktop-runtime.json'),'utf8')).replace(/^\uFEFF/,''))
const tools=await loadLanguageTools(root,manifest)
console.log(JSON.stringify(tools?.criticalFiles||[]))

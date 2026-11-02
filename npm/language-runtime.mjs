import {readFile, realpath, stat} from 'node:fs/promises'
import {isAbsolute, relative, resolve, sep, win32} from 'node:path'

const registryName='runtimes/language-tools.json'
function parts(value){
  if(typeof value!=='string'||!value||value.includes(':')||isAbsolute(value)||win32.isAbsolute(value)||/[\u0000-\u001f]/.test(value))throw Error('语言服务包路径无效。')
  const items=value.replaceAll('\\','/').split('/')
  if(items.some(item=>!item||item==='.'||item==='..'))throw Error('语言服务包路径超出安装目录。')
  return items
}
function inside(root,path){const name=relative(root,path);return name!==''&&name!=='..'&&!name.startsWith('..'+sep)&&!isAbsolute(name)}

/** Validate every declared server and SDK without launching processes or downloads. */
export async function loadLanguageTools(root,manifest){
  if(manifest.languageTools===undefined)return null
  if(manifest.languageTools!==registryName)throw Error('安装包的语言服务清单路径无效。')
  root=await realpath(root)
  const critical=new Set()
  async function path(value,kind='any'){
    const items=parts(value)
    if(items[0]!=='runtimes'||items.length<2)throw Error('语言服务必须来自安装包运行目录。')
    const candidate=await realpath(resolve(root,...items))
    if(!inside(resolve(root,'runtimes'),candidate))throw Error('语言服务依赖了安装目录外的文件。')
    const info=await stat(candidate)
    if(kind==='file'?!info.isFile():kind==='directory'?!info.isDirectory():!info.isFile()&&!info.isDirectory())throw Error('安装包的语言服务组件类型无效。')
    if(info.isFile())critical.add(items.join('/'))
    return candidate
  }
  function dataPath(value){const items=parts(value);if(items[0]==='language-servers')items.shift();if(!items.length)throw Error('语言服务缓存必须按服务分隔。')}
  async function template(value,depth=0){
    if(depth>24)throw Error('语言服务配置嵌套过深。')
    if(Array.isArray(value)){if(value.length>2048)throw Error('语言服务配置过大。');for(const item of value)await template(item,depth+1);return}
    if(value&&typeof value==='object'){
      const keys=Object.keys(value),types=keys.filter(key=>key==='path'||key==='dataPath')
      if(types.length===1&&(!value[types[0]]||typeof value[types[0]]!=='object'||Array.isArray(value[types[0]]))&&keys.every(key=>[types[0],'prefix','suffix'].includes(key))){
        for(const text of [value.prefix??'',value.suffix??''])if(typeof text!=='string'||text.length>512||/[\u0000-\u001f]/.test(text))throw Error('语言服务路径参数无效。')
        if((value.suffix||'').includes(':')||(value.suffix||'').replaceAll('\\','/').split('/').includes('..'))throw Error('语言服务路径参数越界。')
        if(types[0]==='path')await path(value.path);else dataPath(value.dataPath)
        return
      }
      for(const item of Object.values(value))await template(item,depth+1)
    }
  }
  try{
    const registryPath=await path(registryName,'file'),raw=await readFile(registryPath)
    if(raw.length>1024*1024)throw Error('语言服务清单过大。')
    const registry=JSON.parse(raw.toString('utf8').replace(/^\uFEFF/,''))
    if(registry.schemaVersion!==1||!Array.isArray(registry.servers)||!registry.servers.length||registry.servers.length>128)throw Error('语言服务清单无效。')
    const ids=new Set()
    for(const server of registry.servers){
      if(!server||typeof server.id!=='string'||!/^[a-z][a-z0-9_-]{0,79}$/.test(server.id)||ids.has(server.id))throw Error('语言服务清单包含无效或重复的服务。')
      ids.add(server.id)
      await path(server.command,'file')
      if(!Array.isArray(server.args??[])||(server.args||[]).length>2048)throw Error('语言服务启动参数无效。')
      for(const arg of server.args||[]){if(typeof arg!=='string'&&(!arg||typeof arg!=='object'||Array.isArray(arg)||!(typeof arg.path==='string'||typeof arg.dataPath==='string')))throw Error('语言服务启动参数无效。');await template(arg)}
      if(typeof(server.version??'')!=='string'||(server.version||'').length>160)throw Error('语言服务版本无效。')
      for(const [field,kind] of [['requiredFiles','file'],['requiredDirectories','directory']]){
        if(!Array.isArray(server[field]??[])||(server[field]||[]).length>8192)throw Error('语言服务依赖清单无效。')
        for(const item of server[field]||[])await path(item,kind)
      }
      const env=server.env||{}
      if(typeof env!=='object'||Array.isArray(env)||Object.keys(env).length>128)throw Error('语言服务环境配置无效。')
      for(const [key,value] of Object.entries(env)){
        if(!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key))throw Error('语言服务环境变量名称无效。')
        if(value&&typeof value==='object'&&'paths'in value){
          if(!Array.isArray(value.paths)||value.paths.length>128||Object.keys(value).some(k=>!['paths','inherit'].includes(k))||(value.inherit!==undefined&&typeof value.inherit!=='boolean'))throw Error('语言服务搜索路径无效。')
          for(const item of value.paths)await path(item,'directory')
        }else{
          if(typeof value!=='string'&&(!value||typeof value!=='object'||Array.isArray(value)||!(typeof value.path==='string'||typeof value.dataPath==='string')))throw Error('语言服务环境值无效。')
          await template(value)
        }
      }
      if(server.initializationOptions!==undefined&&(!server.initializationOptions||typeof server.initializationOptions!=='object'||Array.isArray(server.initializationOptions)))throw Error('语言服务初始化配置无效。')
      await template(server.initializationOptions||{})
    }
    return {path:registryPath,servers:[...ids],criticalFiles:[...critical]}
  }catch(error){throw Error('安装包的语言分析服务不完整，请重新运行安装程序。'+error.message)}
}

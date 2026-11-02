import assert from 'node:assert/strict'
import test from 'node:test'
import { addDiscoveredModels, localModelEndpoint, modelCapacity, modelProviderDraft, modelProviderPayload } from '../src/lib/modelProviders.js'

test('provider drafts never copy secret descriptors and retain catalog names and current default', () => {
  const source={provider:'deepseek',name:'工作连接',api_key:'must-not-read',api_key_set:true,model_default:'selected',models:[{id:'other',name:'已有名称'}],proxy_mode:'direct'}
  const draft=modelProviderDraft(source)
  assert.equal(draft.api_key,'');assert.equal(draft.proxy_mode,'direct');assert.deepEqual(draft.models,[{id:'selected',name:''},{id:'other',name:'已有名称'}])
  draft.models[1].name='未保存草稿';assert.equal(source.models[0].name,'已有名称')
})

test('a blank key preserves the server credential while multiple models and profile capacities save together', () => {
  const draft=modelProviderDraft({provider:'deepseek',name:'工作连接',model_default:'flash',models:[{id:'flash',name:'Flash'},{id:'pro',name:'Pro'}]})
  draft.context_window=' 131072 ';draft.max_output_tokens='8192';draft.model_default='pro'
  const body=modelProviderPayload(draft)
  assert.equal(Object.hasOwn(body,'api_key'),false);assert.equal(body.models.length,2);assert.equal(body.model_default,'pro');assert.equal(body.context_window,131072)
  draft.context_window='';draft.max_output_tokens=' ';const auto=modelProviderPayload(draft);assert.equal(auto.context_window,null);assert.equal(auto.max_output_tokens,null)
})

for(const value of ['0','1.5','-2','2e3','10000001',true])test(`invalid capacity ${String(value)} is rejected`,()=>assert.throws(()=>modelCapacity(value,'容量'),/整数/))

test('duplicate IDs, missing models, default outside catalog and insufficient output reservation block saving',()=>{
  const draft=modelProviderDraft({provider:'deepseek',name:'连接',model_default:'one',models:[{id:'one',name:'One'}]})
  draft.models.push({id:' one ',name:''});assert.throws(()=>modelProviderPayload(draft),/重复/)
  draft.models=[];assert.throws(()=>modelProviderPayload(draft),/至少/)
  draft.models=[{id:'one',name:''}];draft.model_default='missing';assert.throws(()=>modelProviderPayload(draft),/默认模型/)
  draft.model_default='one';draft.context_window=4096;draft.max_output_tokens=8192;assert.throws(()=>modelProviderPayload(draft),/小于/)
})

test('local endpoint detection supports IPv6 and private subnets without matching unrelated public hosts',()=>{
  for(const url of ['http://[::1]:1234/v1','http://localhost/v1','http://172.31.3.2/v1','http://server.internal/v1'])assert.equal(localModelEndpoint(url),true,url)
  for(const url of ['https://example.com','http://172.32.1.1','http://127.example.com','broken'])assert.equal(localModelEndpoint(url),false,url)
})

test('URL and credential validation does not accept passwords, redirect query parameters or environment assignments',()=>{
  const draft=modelProviderDraft({provider:'openai_compat',name:'连接',model_default:'one'})
  for(const url of ['file:///tmp','https://name:pass@example.com/v1','https://example.com/v1?token=secret','https://example.com/#fragment']){draft.base_url=url;assert.throws(()=>modelProviderPayload(draft),/服务地址/)}
  draft.base_url='http://[::1]:1234/v1';assert.equal(modelProviderPayload(draft).base_url,draft.base_url)
  for(const key of ['API_KEY=not-a-key','"quoted"','bad key','含中文']){draft.api_key=key;assert.throws(()=>modelProviderPayload(draft),/API 密钥/)}
})

test('adopting selected discoveries preserves existing custom names and never adds unselected models',()=>{
  const current=[{id:'one',name:'自己的名称'}], candidates=[{id:'one',name:'服务端名称'},{id:'two',name:'第二个'},{id:'three',name:'第三个'}]
  const result=addDiscoveredModels(current,candidates,new Set(['one','two']))
  assert.deepEqual(result,[{id:'one',name:'自己的名称'},{id:'two',name:'第二个'}]);assert.deepEqual(current,[{id:'one',name:'自己的名称'}])
})

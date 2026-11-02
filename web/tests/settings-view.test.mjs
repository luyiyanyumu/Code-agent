import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { createDialogService } from '../src/composables/useDialog.js'
import * as helpers from '../src/lib/modelProviders.js'

async function setup(t, { active = true, readonly = false, deferProbe = false, failActivate = false } = {}) {
  const calls=[],failures=new Map(),pendingProbes=[],dialog=createDialogService()
  const cfg={provider:'deepseek',base_url:'https://api.deepseek.com',model_default:'flash',active_model:'flash',api_key_set:true,proxy_mode:'system',context_window:131072,max_output_tokens:8192}
  const profile={id:'ds',name:'DeepSeek',provider:'deepseek',base_url:cfg.base_url,model_default:'flash',api_key_set:true,proxy_mode:'direct',active,models:[{id:'flash',name:'Flash'},{id:'pro',name:'Pro'}],context_window:131072,max_output_tokens:8192}
  let store=[profile],disposed
  const props=reactive({readonly,visible:true,focusAnchor:''})
  const request=async(method,path,body)=>{
    calls.push({method,path,body:body?JSON.parse(JSON.stringify(body)):undefined})
    if(failures.has(path))throw Error(failures.get(path))
    if(path==='/api/v1/model-profiles'&&method==='GET')return store
    if(path.endsWith('/probe')){
      if(deferProbe)return new Promise(resolve=>pendingProbes.push(()=>resolve({models:['remote-new']})))
      return {models:['flash','pro','remote-new']}
    }
    if(path==='/api/v1/model-profiles'&&method==='POST'){const saved={...body,id:'created',api_key_set:body.use_current||!!body.api_key,active:false};delete saved.api_key;store=[...store,saved];return saved}
    if(path.endsWith('/activate')){
      if(failActivate)throw Error('激活暂时失败')
      const id=path.split('/').at(-2);store=store.map(p=>({...p,active:p.id===id}));const target=store.find(p=>p.id===id);Object.assign(cfg,target,{active_model:target.model_default});return cfg
    }
    if(method==='PUT'){const saved={...store.find(p=>path.endsWith('/'+p.id)),...body};delete saved.api_key;store=store.map(p=>p.id===saved.id?saved:p);return saved}
    if(method==='DELETE'){const id=path.split('/').at(-1);store=store.filter(p=>p.id!==id);return {ok:true}}
    throw Error(`Unexpected fixture: ${method} ${path}`)
  }
  const source=(await readFile(new URL('../src/components/settings/ModelProviders.vue',import.meta.url),'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm,'')
  const bindings={computed,nextTick,reactive,ref,watch,...helpers,defineProps:()=>props,defineExpose(){},useDialog:()=>dialog,
    api:{modelSettings:async()=>({...cfg})},request,onMounted(){},onBeforeUnmount(fn){disposed=fn}}
  const scope=effectScope()
  const expose='{cfg,profiles,rows,draft,drafts,expanded,advanced,editError,pending,discovered,discoverOpen,selected,pendingActivation,reload,openRow,startAdd,openAddMode,changePreset,preset,save,makeDefault,deleteProvider,probeModels,adoptSelected,cancelEdit}'
  const s=scope.run(()=>new Function(...Object.keys(bindings),`${source}\nreturn ${expose}`)(...Object.values(bindings)))
  t.after(()=>{disposed?.();scope.stop()})
  await s.reload()
  return{s,calls,cfg,profile,dialog,failures,pendingProbes,props,setActivateFailure:value=>{failActivate=value}}
}

test('active provider saves multiple models and capacities atomically while preserving ID and blank key',async t=>{
  const{s,calls}=await setup(t)
  s.draft.value.models[1].name='自己的 Pro';s.draft.value.model_default='pro';s.draft.value.context_window='262144';s.draft.value.max_output_tokens='16384'
  await s.save();const saved=calls.find(c=>c.method==='PUT');assert.equal(saved.path,'/api/v1/model-profiles/ds');assert.equal(saved.body.models.length,2)
  assert.equal(saved.body.models[1].name,'自己的 Pro');assert.equal(saved.body.model_default,'pro');assert.equal(saved.body.proxy_mode,'direct');assert.equal(saved.body.context_window,262144)
  assert.equal(Object.hasOwn(saved.body,'api_key'),false);assert.equal(calls.some(c=>c.path.endsWith('/models')),false)
})
test('discovery uses saved profile credentials and requires explicit catalog adoption',async t=>{
  const{s,calls}=await setup(t);await s.probeModels();const probe=calls.find(c=>c.path.endsWith('/probe'))
  assert.equal(probe.body.profile_id,'ds');assert.equal(probe.body.proxy_mode,'direct');assert.equal(Object.hasOwn(probe.body,'api_key'),false)
  assert.equal(s.draft.value.models.length,2);assert.equal(s.draft.value.context_window,131072);assert.equal(s.discoverOpen.value,true)
  s.selected.value=new Set(['remote-new']);s.adoptSelected();assert.equal(s.draft.value.models.length,3);assert.equal(s.draft.value.models[1].name,'Pro')
})
test('unprofiled current connection migrates with server credential and activates without exposing its key',async t=>{
  const{s,calls}=await setup(t,{active:false});assert.equal(s.expanded.value,'__current');await s.probeModels();assert.equal(calls.find(c=>c.path.endsWith('/probe')).body.use_current,true)
  await s.save();const create=calls.find(c=>c.method==='POST'&&c.path==='/api/v1/model-profiles');assert.equal(create.body.use_current,true);assert.equal(Object.hasOwn(create.body,'api_key'),false);assert.equal(calls.filter(c=>c.path.endsWith('/activate')).length,1)
})
test('failed migration activation retries saved ID without duplicate creation or lost activation',async t=>{
  const{s,calls,setActivateFailure}=await setup(t,{active:false,failActivate:true});await s.save()
  assert.equal(s.expanded.value,'created');assert.equal(s.pendingActivation.value,'created');assert.match(s.editError.value,/连接已保存.*默认失败/);assert.equal(s.draft.value.api_key,'')
  setActivateFailure(false);await s.save();assert.equal(calls.filter(c=>c.method==='POST'&&c.path==='/api/v1/model-profiles').length,1);assert.equal(calls.filter(c=>c.path.endsWith('/activate')).length,2);assert.equal(s.pendingActivation.value,'')
})
test('failed save preserves editable fields and clearing capacities restores automatic limits',async t=>{
  const{s,calls,failures}=await setup(t);s.draft.value.context_window='';s.draft.value.max_output_tokens=' ';s.draft.value.models[0].name='未保存名称';failures.set('/api/v1/model-profiles/ds','无法保存')
  await s.save();assert.equal(s.editError.value,'无法保存');assert.equal(s.draft.value.models[0].name,'未保存名称');failures.clear();await s.save()
  const saved=calls.findLast(c=>c.method==='PUT');assert.equal(saved.body.context_window,null);assert.equal(saved.body.max_output_tokens,null)
})
test('invalid capacities and duplicate IDs block every mutation',async t=>{
  const{s,calls}=await setup(t);s.draft.value.context_window='1.5';await s.save();assert.match(s.editError.value,/整数/)
  s.draft.value.context_window=131072;s.draft.value.max_output_tokens=131072;await s.save();assert.match(s.editError.value,/小于/)
  s.draft.value.max_output_tokens=8192;s.draft.value.models.push({id:'flash',name:'重复'});await s.save();assert.match(s.editError.value,/重复/);assert.equal(calls.some(c=>c.method!=='GET'),false)
})
test('switching providers and reloading configuration preserve separate unsaved drafts',async t=>{
  const{s}=await setup(t);s.draft.value.api_key='test-only-new-key';s.draft.value.models[0].name='自己的名称';await s.reload();assert.equal(s.draft.value.api_key,'test-only-new-key')
  s.startAdd();s.draft.value.name='第三方草稿';s.openAddMode('custom');s.draft.value.name='自定义草稿';s.openAddMode('third');assert.equal(s.draft.value.name,'第三方草稿')
  s.openRow(s.rows.value.find(row=>row.id==='ds'));assert.equal(s.draft.value.models[0].name,'自己的名称');assert.equal(s.draft.value.api_key,'test-only-new-key')
})
test('pending probes lock route switching and reject stale endpoint responses',async t=>{
  const{s,pendingProbes}=await setup(t,{deferProbe:true});const probe=s.probeModels();s.startAdd();assert.equal(s.expanded.value,'ds');s.draft.value.base_url='http://127.0.0.1:11434/v1';pendingProbes[0]();await probe;assert.equal(s.discovered.value.length,0);assert.equal(s.pending.value,'')
})
test('read-only view cannot mutate providers, discover models or switch defaults',async t=>{
  const{s,calls}=await setup(t,{readonly:true});await s.save();await s.probeModels();await s.makeDefault(s.rows.value[0]);await s.deleteProvider(s.rows.value[0]);assert.equal(calls.some(c=>c.method!=='GET'),false)
})
test('provider deletion cancellation makes no writes and confirmation keeps its original target',async t=>{
  const{s,calls,dialog}=await setup(t);const row={...s.rows.value[0]};const cancelled=s.deleteProvider(row);dialog.cancel();await cancelled;assert.equal(calls.some(c=>c.method==='DELETE'),false)
  const deletion=s.deleteProvider(row);row.id='other';row.name='Other';assert.equal(dialog.active.value.options.details,'DeepSeek');assert.equal(await dialog.submit(),true);await deletion;assert.equal(calls.find(c=>c.method==='DELETE').path,'/api/v1/model-profiles/ds')
})

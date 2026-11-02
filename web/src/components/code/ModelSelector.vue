<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {api} from '../../api.js'
import {rankedModels, reasoningSelection, selectionName} from '../../lib/modelDirectory.js'
import UiIcon from '../ui/UiIcon.vue'

const props = defineProps({sessionId:{type:String,default:''}, visible:{type:Boolean,default:true}, observedModel:{type:String,default:''}})
const emit = defineEmits(['open-settings','selected'])
const directory=ref(null), loading=ref(false), saving=ref(false), error=ref(''), opened=ref(false), pane=ref('root'), query=ref(''), active=ref(0)
const trigger=ref(null), popup=ref(null), search=ref(null), position=ref({}), rows=ref([]), effortRows=ref([])
const rootRow=ref('models')
let generation=0
const label=computed(()=>selectionName(directory.value))
const reasoning=computed(()=>reasoningSelection(directory.value))
const groups=computed(()=>rankedModels(directory.value?.groups,query.value))
const choices=computed(()=>groups.value.flatMap(group=>group.models.map(model=>({group,model}))))
const showSearch=computed(()=>(directory.value?.groups||[]).reduce((n,g)=>n+(g.models?.length||0),0)>4)
const selected=(group,model)=>directory.value?.current?.provider===group.id&&directory.value?.current?.model===model.id
async function load(){
  if(saving.value)return
  const id=props.sessionId, mine=++generation
  if(!id){directory.value=null;loading.value=false;return}
  loading.value=true;error.value=''
  try{const value=await api.sessionModels(id);if(mine===generation&&id===props.sessionId)directory.value=value}
  catch(e){if(mine===generation)error.value=e.message}
  finally{if(mine===generation)loading.value=false}
}
function close(){opened.value=false;query.value='';nextTick(()=>trigger.value?.focus())}
function place(){
  if(!opened.value||!trigger.value)return
  const box=trigger.value.getBoundingClientRect(),width=Math.min(360,innerWidth-24),limit=Math.min(360,innerHeight-24),height=Math.min(popup.value?.scrollHeight||limit,limit)
  const top=box.top>=height+8?box.top-height-8:Math.min(box.bottom+8,innerHeight-height-12)
  position.value={width:width+'px',maxHeight:limit+'px',left:Math.max(12,Math.min(box.right-width,innerWidth-width-12))+'px',top:Math.max(12,top)+'px'}
}
async function focusCurrent(){
  await nextTick();place()
  if(pane.value==='models'){
    active.value=Math.max(0,choices.value.findIndex(({group,model})=>selected(group,model)))
    if(showSearch.value)search.value?.focus();else rows.value[active.value]?.focus()
  }else if(pane.value==='effort'){
    active.value=Math.max(0,reasoning.value?.choices.findIndex(choice=>choice.id===reasoning.value.id)??0)
    effortRows.value[active.value]?.focus()
  }else popup.value?.querySelector(`[data-pane="${rootRow.value}"]`)?.focus()
}
function enterPane(value){rootRow.value=value;pane.value=value;focusCurrent()}
function back(){pane.value='root';query.value='';focusCurrent()}
async function openMenu(models=false){
  if(!props.sessionId)return
  opened.value=true;pane.value=models?'models':'root';rootRow.value='models';query.value='';place()
  await load();await focusCurrent()
}
async function choose(selection){
  if(saving.value)return
  const id=props.sessionId,mine=++generation
  saving.value=true;loading.value=false;error.value=''
  try{
    const value=await api.selectSessionModel(id,selection)
    if(mine!==generation||id!==props.sessionId)return
    directory.value=value;emit('selected',value.current);close()
  }catch(e){if(mine===generation)error.value=e.message}
  finally{if(mine===generation)saving.value=false}
}
function move(delta){
  const effort=pane.value==='effort',length=effort?reasoning.value?.choices.length:choices.value.length
  if(!length)return
  active.value=(active.value+delta+length)%length
  nextTick(()=>{const row=(effort?effortRows.value:rows.value)[active.value];row?.scrollIntoView({block:'nearest'});if(effort||!showSearch.value)row?.focus()})
}
function chooseEffort(choice){
  const current=directory.value?.current
  if(current)choose({provider:current.provider,model:current.model,...(choice.id?{reasoningEffort:choice.id}:{})})
}
function keydown(event){
  if(event.key==='Escape'||(event.key==='Tab'&&event.shiftKey)){
    event.preventDefault();if(pane.value!=='root')back();else close();return
  }
  if(pane.value==='root'&&['ArrowDown','ArrowUp'].includes(event.key)){
    event.preventDefault();const buttons=[...popup.value.querySelectorAll('button')],index=buttons.indexOf(document.activeElement)
    buttons[(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();return
  }
  if(pane.value==='root'||saving.value||event.target.closest?.('.model-manage, header, .model-error, .model-search button'))return
  if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();move(event.key==='ArrowDown'?1:-1)}
  else if(['Enter','Tab'].includes(event.key)){
    if(pane.value==='effort'){
      const item=reasoning.value?.choices[active.value]
      if(item){event.preventDefault();chooseEffort(item)}
    }else{
      const item=choices.value[active.value]
      if(item){event.preventDefault();choose({provider:item.group.id,model:item.model.id})}
    }
  }
}
function outside(event){if(opened.value&&!popup.value?.contains(event.target)&&!trigger.value?.contains(event.target))opened.value=false}
function settings(){close();emit('open-settings','models')}
watch(query,()=>{active.value=0;nextTick(place)})
watch(error,()=>nextTick(place))
watch(()=>props.sessionId,()=>{generation++;opened.value=false;saving.value=false;directory.value=null;load()},{immediate:true})
watch(()=>props.visible,visible=>{if(!visible)opened.value=false;else if(props.sessionId)load()})
watch(()=>props.observedModel,model=>{if(directory.value&&!saving.value&&model&&model!==directory.value.current?.model)load()})
onMounted(()=>{document.addEventListener('pointerdown',outside);window.addEventListener('resize',place)})
onBeforeUnmount(()=>{generation++;document.removeEventListener('pointerdown',outside);window.removeEventListener('resize',place)})
defineExpose({open:()=>openMenu(true),refresh:load})
</script>

<template>
  <div class="model-seat">
    <button ref="trigger" class="model-trigger" :class="{unavailable:directory?.available===false}" :disabled="!sessionId||saving" aria-haspopup="dialog" :aria-expanded="opened" :aria-label="`选择会话模型：${label}${reasoning?'，推理强度：'+reasoning.name:''}`" :title="directory?.reason||'只影响本会话，从下一次请求生效'" @click="opened?close():openMenu()">
      <UiIcon name="server" :size="14"/><span>{{label}}</span><span v-if="reasoning" class="effort-caption">{{reasoning.name}}</span><span v-if="loading||saving" aria-label="处理中">…</span><span v-else aria-hidden="true">⌄</span>
    </button>
    <Teleport to="body">
      <div v-if="opened" ref="popup" class="model-surface" :style="position" role="dialog" aria-label="会话模型选择" @keydown="keydown">
        <div v-if="pane==='root'" class="model-root">
          <button class="model-root-row" data-pane="models" @click="enterPane('models')"><span>模型</span><span class="value">{{label}}</span><span aria-hidden="true">›</span></button>
          <button v-if="reasoning" class="model-root-row" data-pane="effort" @click="enterPane('effort')"><span>推理强度</span><span class="value">{{reasoning.name}}</span><span aria-hidden="true">›</span></button>
          <p class="selection-note">仅切换本会话，下一次请求生效。</p>
          <button class="model-manage" @click="settings">管理模型提供方 <span aria-hidden="true">↗</span></button>
        </div>
        <template v-else>
          <header><button aria-label="返回模型菜单" @click="back">‹</button><span>{{pane==='effort'?'推理强度':'选择模型'}}</span><button aria-label="关闭模型选择" @click="close">×</button></header>
          <div v-if="pane==='effort'" class="model-groups effort-list" role="listbox" aria-label="推理强度">
            <button v-for="(choice,index) in reasoning?.choices||[]" :key="choice.id" :ref="el=>{if(el)effortRows[index]=el}" class="model-choice" :class="{highlighted:active===index}" role="option" :aria-selected="choice.id===reasoning.id" :disabled="saving" @focus="active=index" @mousemove="active=index" @click="chooseEffort(choice)"><span class="effort-text"><span>{{choice.name}}</span><small v-if="choice.description">{{choice.description}}</small></span><UiIcon v-if="choice.id===reasoning.id" name="check" :size="14"/></button>
          </div>
          <template v-else>
          <div v-if="showSearch" class="model-search"><input ref="search" v-model="query" type="search" placeholder="搜索模型…" aria-label="搜索模型" :aria-activedescendant="choices.length?'model-choice-'+active:undefined"/><button v-if="query" aria-label="清空模型搜索" @click="query='';search?.focus()">×</button></div>
          <div v-if="loading&&!directory" class="model-empty" role="status">正在读取模型…</div>
          <div v-else class="model-groups" role="listbox" aria-label="可用模型">
            <section v-for="group in groups" :key="group.id" :aria-label="group.name">
              <h4>{{group.name}}<span v-if="!group.configured">未配置密钥</span></h4>
              <button v-for="model in group.models" :id="'model-choice-'+choices.findIndex(c=>c.group.id===group.id&&c.model.id===model.id)" :key="model.id" :ref="el=>{if(el)rows[choices.findIndex(c=>c.group.id===group.id&&c.model.id===model.id)]=el}" class="model-choice" :class="{highlighted:choices[active]?.group.id===group.id&&choices[active]?.model.id===model.id}" role="option" :aria-selected="selected(group,model)" :disabled="saving" :title="model.id" @focus="active=choices.findIndex(c=>c.group.id===group.id&&c.model.id===model.id)" @mousemove="active=choices.findIndex(c=>c.group.id===group.id&&c.model.id===model.id)" @click="choose({provider:group.id,model:model.id})"><span>{{model.name||model.id}}</span><UiIcon v-if="selected(group,model)" name="check" :size="14"/></button>
            </section>
            <p v-if="!choices.length" class="model-empty" role="status">{{query?'没有匹配的模型':'尚未配置模型提供方'}}</p>
          </div>
          <button class="model-manage" @click="choose({provider:'',model:''})">跟随全局默认<span v-if="directory?.inherited"> ✓</span></button>
          <button class="model-manage" @click="settings">管理模型提供方 ↗</button>
          </template>
        </template>
        <div v-if="error" class="model-error" role="alert">{{error}}<button :disabled="loading" @click="load">重新读取</button></div>
        <p v-else-if="directory?.available===false" class="model-error">{{directory.reason||'当前模型不可用，请选择其他模型或检查提供方。'}}</p>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.model-seat{min-width:0;max-width:100%}.model-trigger{display:flex;align-items:center;gap:6px;max-width:100%;min-height:28px;padding:3px 6px;border:0;background:transparent;font-size:var(--fs-xs);font-weight:400;color:var(--text-2)}.model-trigger>span:first-of-type{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.model-trigger:hover{background:var(--hover)}.model-trigger.unavailable{color:var(--warn)}
.model-surface{position:fixed;z-index:1600;display:flex;flex-direction:column;overflow:hidden;border:1px solid var(--border);border-radius:10px;background:var(--panel);color:var(--text);box-shadow:var(--shadow-pop);font-size:13px}.model-surface button{font-size:13px;font-weight:400;border:0;border-radius:5px;background:transparent;color:inherit;text-align:left;cursor:pointer}.model-surface button:focus-visible,.model-surface button:hover{background:var(--hover);outline:2px solid var(--accent);outline-offset:-2px}.model-root{padding:6px}.model-root-row{display:flex;align-items:center;gap:12px;width:100%;min-height:38px;padding:8px}.value{margin-left:auto;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}.selection-note{margin:4px 8px 10px;font-size:11px;color:var(--muted)}.model-surface header{display:flex;align-items:center;gap:8px;padding:6px;border-bottom:1px solid var(--border);flex:none}.model-surface header button{min-width:28px;min-height:28px;text-align:center}.model-surface header button:last-child{margin-left:auto}.model-search{display:flex;align-items:center;padding:6px 10px;border-bottom:1px solid var(--border);flex:none}.model-search input{width:100%;min-width:0;background:transparent;border:0;box-shadow:none;font-size:13px;padding:6px 0}.model-search button{padding:4px 6px}.model-groups{overflow:auto;min-height:0;padding:4px 6px;overscroll-behavior:contain}.model-groups section h4{position:sticky;top:-4px;margin:0;padding:7px 8px;color:var(--muted);font-size:11px;font-weight:500;background:var(--panel);z-index:1}.model-groups h4 span{float:right;color:var(--warn);font-weight:400}.model-choice{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;min-height:34px;padding:7px 9px}.model-choice span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.model-choice.highlighted{background:var(--hover)}.model-manage{flex:none;display:block;width:100%;padding:9px 12px!important;border-top:1px solid var(--border)!important;border-radius:0!important;color:var(--muted)!important}.model-empty{padding:16px 8px;margin:0;color:var(--muted);text-align:center}.model-error{flex:none;margin:0;padding:9px 12px;color:var(--err);font-size:12px;overflow-wrap:anywhere;border-top:1px solid var(--border)}.model-error button{margin-left:8px;color:var(--accent);text-decoration:underline}
.model-search input::-webkit-search-cancel-button{-webkit-appearance:none;appearance:none}
.effort-caption{flex:none;padding-left:6px;border-left:1px solid var(--border);color:var(--muted);font-size:11px}.effort-list{padding:6px}.effort-text{display:flex;flex-direction:column;gap:3px;min-width:0}.effort-text small{white-space:normal;color:var(--muted);line-height:1.5;font-size:11px}
</style>

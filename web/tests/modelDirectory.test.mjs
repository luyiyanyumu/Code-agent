import test from 'node:test'
import assert from 'node:assert/strict'
import {rankedModels,reasoningSelection,selectionName} from '../src/lib/modelDirectory.js'

test('model search keeps provider groups, ranks prefixes and supports ordered fuzzy names',()=>{
  const groups=[{id:'a',name:'A',models:[{id:'1',name:'Custom DeepSeek'},{id:'2',name:'DeepSeek Chat'},{id:'3',name:'Other'}]},
    {id:'b',name:'B',models:[{id:'4',name:'DeepSeek Reasoner'}]}]
  assert.deepEqual(rankedModels(groups,' deepseek ').map(g=>[g.id,g.models.map(m=>m.id)]),[['a',['2','1']],['b',['4']]])
  assert.equal(rankedModels(groups,'dskcht')[0].models[0].id,'2')
  assert.deepEqual(rankedModels(groups,'unmatched'),[])
  assert.equal(groups[0].models[0].id,'1','search must not mutate the source catalog')
})

test('deleted provider/model keeps the saved model ID without choosing another catalog entry',()=>{
  assert.equal(selectionName({groups:[],current:{provider:'removed',model:'my-saved-model'}}),'my-saved-model')
  assert.equal(selectionName({groups:[{id:'a',models:[{id:'x',name:'Friendly name'}]}],current:{provider:'a',model:'x'}}),'Friendly name')
  assert.equal(selectionName(null),'选择模型')
})

test('reasoning choices follow exact catalog capability and retain unavailable saved effort',()=>{
  const directory={current:{provider:'a',model:'x'},groups:[{id:'a',models:[{id:'x',reasoning:{efforts:[{id:'off',name:'关闭'},{id:'high',name:'高'}],defaultEffort:'high'}}]}]}
  assert.equal(reasoningSelection(directory).name,'高')
  directory.current.reasoningEffort='off'
  assert.equal(reasoningSelection(directory).name,'关闭')
  directory.current.reasoningEffort='removed'
  assert.equal(reasoningSelection(directory).name,'removed','never silently replace a saved effort')
  delete directory.current.reasoningEffort
  delete directory.groups[0].models[0].reasoning.defaultEffort
  assert.deepEqual(reasoningSelection(directory).choices[0],{id:'',name:'服务商默认'})
  assert.equal(reasoningSelection(directory).id,'')
  directory.current.model='unknown'
  assert.equal(reasoningSelection(directory),null)
  assert.equal(reasoningSelection(null),null)
})

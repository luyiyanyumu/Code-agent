<script setup>
defineProps({ busy: Boolean, hasSession: Boolean })
defineEmits(['start'])
const starters = [
  { title: '梳理想法', description: '从零散的念头，找到清晰的方向', icon: 'M4 3h10v12H4zM7 6h4M7 9h4M7 12h2', prompt: '我有一些零散的想法，请和我一起梳理目标、问题和下一步。' },
  { title: '写作与表达', description: '起草、润色，找到合适的表达', icon: 'M3 13l1-4 8-8 4 4-8 8-5 1zM10 3l4 4M3 16h12', prompt: '请帮我写一份内容。我会告诉你主题、读者和希望采用的语气。' },
  { title: '学习与探索', description: '理解新概念，把问题聊明白', icon: 'M2 4h5l2 2 2-2h5v11h-5l-2 1-2-1H2zM9 6v10', prompt: '我想了解一个新主题，请从容易理解的概念和例子开始，和我一起探索。' },
]
</script>

<template>
  <div class="welcome-state">
    <div class="welcome-symbol" aria-hidden="true"><svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 4h16v12H11l-6 5v-5H4z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" /></svg></div>
    <div class="welcome-eyebrow">一起把想法变成结果</div>
    <h1>{{ hasSession ? '开始新的对话' : '今天想聊些什么？' }}</h1>
    <p class="welcome-desc">选择一个起点，或在下方描述你的任务。</p>
    <div class="welcome-starters">
      <button v-for="item in starters" :key="item.title" :disabled="busy" @click="$emit('start', item.prompt)">
        <svg viewBox="0 0 18 18" width="19" height="19" aria-hidden="true"><path :d="item.icon" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" /></svg>
        <span><b>{{ item.title }}</b><small>{{ item.description }}</small></span>
        <span class="welcome-arrow" aria-hidden="true">↗</span>
      </button>
    </div>
    <button v-if="!hasSession" class="primary welcome-new" :disabled="busy" @click="$emit('start', '')">{{ busy ? '正在创建…' : '开始新对话' }}<span aria-hidden="true">＋</span></button>
    <p class="welcome-note">{{ hasSession ? '在下方输入你的任务，或选择一个起点。' : '也可以打开会话列表，继续之前的工作。' }}</p>
  </div>
</template>

<style scoped>
.welcome-state { width: 100%; max-width: 670px; margin: auto; padding: 44px 0 32px; text-align: center; }
.welcome-symbol { display: grid; place-items: center; width: 58px; height: 58px; margin: 0 auto 22px; background: #202c45; color: #fff; border-radius: 17px; box-shadow: 0 8px 24px #202c4512; }
.welcome-eyebrow { color: var(--muted); font-size: var(--fs-xs); margin-bottom: 10px; }
h1 { margin: 0; font-size: 30px; line-height: 1.35; font-weight: 650; letter-spacing: -.035em; }
.welcome-desc { margin: 12px 0 32px; color: var(--muted); font-size: var(--fs-sm); }
.welcome-starters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; text-align: left; }
.welcome-starters button { min-height: 126px; position: relative; display: flex; flex-direction: column; align-items: flex-start; justify-content: flex-start; padding: 18px; gap: 12px; background: var(--panel); border-color: var(--border); border-radius: var(--r-md); white-space: normal; text-align: left; transition: border-color .15s, background .15s, transform .15s; }
.welcome-starters button:hover { border-color: var(--accent-border); background: var(--panel-soft); transform: translateY(-2px); }
.welcome-starters svg { flex: none; color: var(--accent); }
.welcome-starters b { display: block; font-size: var(--fs-sm); font-weight: 600; margin-bottom: 5px; }
.welcome-starters small { display: block; font-size: var(--fs-xs); color: var(--muted); line-height: 1.65; }
.welcome-arrow { position: absolute; right: 16px; top: 15px; color: var(--faint); }
.welcome-new { margin-top: 28px; gap: 22px; padding: 0 20px; min-height: 38px; border-radius: 9px; }
.welcome-note { margin: 16px 0 0; font-size: var(--fs-xs); color: var(--muted); }
@media (max-width: 1100px) { .welcome-starters { gap: 8px; }.welcome-starters button { padding: 14px; } }
@media (max-width: 600px) {
  .welcome-state { padding: 16px 0; }.welcome-symbol { width: 40px; height: 40px; margin-bottom: 12px; border-radius: 12px; } h1 { font-size: 24px; }.welcome-desc { margin-bottom: 20px; }
  .welcome-starters { grid-template-columns: 1fr; gap: 8px; }.welcome-starters button { min-height: 58px; flex-direction: row; align-items: center; padding: 10px 16px; gap: 12px; }.welcome-starters b { margin-bottom: 1px; }.welcome-arrow { position: static; margin-left: auto; }
  .welcome-new { margin-top: 20px; }
}
</style>

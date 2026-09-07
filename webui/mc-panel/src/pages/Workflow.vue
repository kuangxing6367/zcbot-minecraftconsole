<template>
  <div>
    <!-- 顶部：打开编辑器 -->
    <div class="mc-card" style="padding:20px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:16px">
      <div>
        <div class="mc-title" style="font-size:16px">工作流编排</div>
        <div class="mc-sub" style="margin-top:4px">把「聊天关键词 → 检查 → 对服务器执行命令」串成可视化流程。编辑器为独立全屏应用，将新窗口打开。</div>
      </div>
      <el-button type="primary" size="large" data-test="wf-open"
                 @click="openEditor">打开工作流编辑器（新窗口）</el-button>
    </div>

    <!-- 通俗说明卡 -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px">
      <div class="mc-card" style="padding:18px 20px">
        <div style="font-size:22px">🫀</div>
        <div class="mc-title" style="margin:8px 0 6px">触发 Trigger</div>
        <div class="mc-sub" style="line-height:1.9">流程从哪开始。通常是群聊/私聊里的一句话或关键词，例如玩家发「绑定 xxx」。</div>
      </div>
      <div class="mc-card" style="padding:18px 20px">
        <div style="font-size:22px">🔍</div>
        <div class="mc-title" style="margin:8px 0 6px">条件 Condition</div>
        <div class="mc-sub" style="line-height:1.9">判断上一步返回是否符合（包含/正则），是→走「真」分支，否→「假」分支，实现"没权限就拒绝"这类分流。</div>
      </div>
      <div class="mc-card" style="padding:18px 20px">
        <div style="font-size:22px">⚙️</div>
        <div class="mc-title" style="margin:8px 0 6px">动作 Action</div>
        <div class="mc-sub" style="line-height:1.9">真正往 MC 发命令。支持 `{last}`（上一步输出）与 `{player}`（绑定玩家）占位自动替换。</div>
      </div>
      <div class="mc-card" style="padding:18px 20px">
        <div style="font-size:22px">🏁</div>
        <div class="mc-title" style="margin:8px 0 6px">结束 End</div>
        <div class="mc-sub" style="line-height:1.9">收尾并拼接各动作结果，作为最终回复发回给触发人。</div>
      </div>
    </div>

    <div class="mc-card" style="padding:16px 20px;margin-top:16px">
      <div class="mc-title" style="margin-bottom:8px">小提示</div>
      <ul style="margin:0;padding-left:20px;color:var(--mc-sub);line-height:2;font-size:13px">
        <li>权限：工作流按 <code>zcbot.wf.&lt;id&gt;</code> 权限节点放行，默认组已全开（<code>zcbot.wf.*</code>）。</li>
        <li>占位：动作命令里的 <code>{last}</code>=上一步输出、<code>{player}</code>=触发人绑定的 MC 玩家（未绑定会提示先 <code>/绑定玩家</code>）。</li>
        <li>编辑器保存后回到面板点「工作流」列表即可看到启用状态（若之后要在此处做管理列表，我再补）。</li>
      </ul>
    </div>
  </div>
</template>

<script setup>
function openEditor() {
  window.open('/minecraftconsole/workflow.html', '_blank', 'noopener')
}
</script>

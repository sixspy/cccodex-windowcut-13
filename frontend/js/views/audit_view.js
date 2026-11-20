export const auditViewTemplate = `
<div class="card" v-show="tab===5">
  <h2>审计互证（五处同态）</h2>
  <p class="muted">
    口径：甲路径同事务强制审计（见 docs/审计互证册.md）。
    互证状态：
    <b :style="proof.aligned ? 'color:#15803d' : 'color:#b91c1c'">
      {{ proof.aligned ? '全部对齐' : '存在空洞' }}
    </b>
    <button class="ghost" style="margin-left:8px" @click="refresh">重新互证</button>
  </p>
  <table>
    <thead><tr><th>台账族</th><th>业务行数</th><th>审计行数</th><th>空洞</th><th>状态</th></tr></thead>
    <tbody>
      <tr v-for="f in proof.families" :key="f.family">
        <td>{{f.family}}</td>
        <td>{{f.business_rows}}</td>
        <td>{{f.audit_rows}}</td>
        <td>{{f.gap_count}}</td>
        <td :style="f.gap_count===0 ? 'color:#15803d' : 'color:#b91c1c'">
          {{f.gap_count===0 ? '对齐' : '对不上'}}
        </td>
      </tr>
    </tbody>
  </table>
  <p class="muted" v-if="!proof.aligned">
    空洞明细：<span v-for="g in proofGaps" :key="g.family+g.business_id">
      [{{g.family}} #{{g.business_id}}]
    </span>
  </p>
</div>
<div class="card" v-show="tab===5">
  <h2>动作汇总（与台账互证）</h2>
  <table>
    <thead><tr><th>动作</th><th>次数</th></tr></thead>
    <tbody>
      <tr v-for="a in actionSummary" :key="a.action">
        <td>{{a.action}}</td><td>{{a.c}}</td>
      </tr>
    </tbody>
  </table>
</div>
<div class="card" v-show="tab===5">
  <h2>审计流水</h2>
  <table>
    <thead><tr><th>时间</th><th>人</th><th>动作</th><th>目标</th></tr></thead>
    <tbody>
      <tr v-for="a in audits.slice(0,20)" :key="a.id">
        <td>{{a.created_at}}</td>
        <td>{{a.actor}}</td>
        <td>{{a.action}}</td>
        <td>{{a.target}}</td>
      </tr>
    </tbody>
  </table>
</div>
<div class="card" v-show="tab===5">
  <h2>登录会话</h2>
  <table>
    <thead><tr><th>ID</th><th>用户</th><th>角色</th><th>状态</th><th>登录时间</th><th>退出时间</th></tr></thead>
    <tbody>
      <tr v-for="s in sessions" :key="s.id">
        <td>{{s.id}}</td><td>{{s.username}}</td><td>{{s.role}}</td>
        <td>{{s.status}}</td><td>{{s.login_at}}</td><td>{{s.logout_at || '—'}}</td>
      </tr>
    </tbody>
  </table>
</div>
<div class="card" v-show="tab===5">
  <h2>争用 / 锁</h2>
  <p class="muted">争用 {{contention.length}} · 持锁 {{locks.length}}</p>
  <table>
    <thead><tr><th>锁键</th><th>角色</th><th>动作</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="c in contention.slice(0,15)" :key="'c'+c.id">
        <td>{{c.lock_key}}</td>
        <td>{{c.actor}}</td>
        <td>{{c.action}}</td>
        <td>{{c.created_at}}</td>
      </tr>
    </tbody>
  </table>
  <h2 style="margin-top:16px">旁路暂存</h2>
  <label>窗口</label><input v-model="holdForm.window_name"/>
  <label>业务键</label><input v-model="holdForm.biz_key"/>
  <label>时间</label><input v-model="holdForm.event_ts"/>
  <label>金额</label><input type="number" v-model.number="holdForm.amount"/>
  <button class="primary" @click="createHold">写入暂存</button>
  <table>
    <thead><tr><th>ID</th><th>键</th><th>金额</th><th>状态</th></tr></thead>
    <tbody>
      <tr v-for="h in holds" :key="'h'+h.id">
        <td>{{h.id}}</td><td>{{h.biz_key}}</td><td>{{h.amount}}</td><td>{{h.status}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;

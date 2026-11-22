import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {billingLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

function bytes(value) {
  return value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN');
}

function monthOptions(state, selected) {
  const known = new Set(state.billing.map(row=>row.month));
  if (selected) known.add(selected);
  return Array.from(known).sort().reverse()
    .map(month=>`<option value="${esc(month)}" ${month===selected?'selected':''}>${esc(month)}</option>`).join('');
}

function label(value) {
  return esc(billingLabels[value] || value || '');
}

export function billingPage(state, identity) {
  const selected = state.billing.find(row=>row.month===state.billingMonth) || state.billing[0];
  const operator = state.session.role==='operator';
  const actualTotal = selected?.groups.reduce((sum,row)=>sum+row.actual_bytes, 0) || 0;
  const billedTotal = selected?.groups.reduce((sum,row)=>sum+row.billed_bytes, 0) || 0;
  const groupRows = (selected?.groups || []).map(row=>[
    esc(row.production_group), bytes(row.actual_bytes), bytes(row.billed_bytes),
    row.matched ? '<span class="status" data-state="complete">已对齐</span>' : '<span class="status" data-state="failed">待补差</span>']);
  const settlementRows = (selected?.settlement_rows || []).map(row=>[
    `#${row.seq}`, label(row.kind), label(row.trigger),
    esc(row.created_at ? date(row.created_at) : ''), esc(row.actor),
    bytes(row.totals?.measured_bytes), esc((row.bills || []).map(x=>`${x.production_group}: ${x.total_bytes}`).join(' · ') || '—')]);
  const recordRows = (selected?.record_rows || []).map(row=>[
    esc(row.record_key), esc(row.production_group), label(row.status),
    esc(row.blocks.length), bytes(row.blocks.reduce((sum,item)=>sum+item.bytes, 0)),
    esc(date(row.created_at)), esc(row.payload?.source?.run_id ? `执行 ${row.payload.source.run_id}` : '手工录入')]);
  const header = `<div class="actions">
    <label>归属月份<select id="billing-month">${monthOptions(state, selected?.month)}</select></label>
    ${operator?'<button class="secondary" type="button" id="billing-close">关账并发布账单</button><button class="secondary" type="button" id="billing-settle">补出差额账单</button>':''}
    </div>`;
  const status = selected?.period ? `<p data-testid="period-status">${label(selected.period.status)} · 结算 ${selected.settlements.length} 批</p>` : '<p class="note">该月份尚未入账，也还没有账单。</p>';
  return `${heading(topic.modules[4], '每块按实测字节在引用它的制作组间平分，同组重复引用只算一次。', header)}
    ${status}
    <dl class="metrics"><div><dt>实测占用字节</dt><dd>${bytes(actualTotal)}</dd></div><div><dt>已出账字节</dt><dd>${bytes(billedTotal)}</dd></div><div><dt>唯一块数</dt><dd>${bytes(selected?.block_count || 0)}</dd></div><div><dt>验收记录</dt><dd>${bytes(selected?.record_count || 0)}</dd></div></dl>
    ${panel('制作组占用', records(['制作组','实占字节','已出账字节','对账'], groupRows, '该月份还没有入账记录。'))}
    ${panel('结算批次', records(['批次','类型','触发','时间','经办','实测字节','各组账单'], settlementRows, '关账后生成原始账单，迟到或撤销生成差额账单。'))}
    ${panel('验收记录', records(['记录键','制作组','状态','块数','实测字节','入账时间','来源'], recordRows, '暂无验收记录。'))}
    ${operator?`<div class="spread"><section class="panel"><h2>登记验收记录</h2>
      <form id="billing-record-form" class="field-grid">
        <label class="field">归属月份<input name="month" type="month" required value="${esc(selected?.month || '')}" /></label>
        <label class="field">制作组<input name="production_group" required maxlength="64" /></label>
        <label class="field">记录键<input name="record_key" required maxlength="128" /></label>
        <label class="field">执行编号（可选）<input name="run_id" type="number" min="1" step="1" /></label>
        <label class="field data"><span>内容块清单</span><textarea name="blocks" rows="6" spellcheck="false">[]</textarea></label>
        <label class="field">备注<input name="note" maxlength="500" /></label>
        <div class="actions"><button>入账</button></div>
      </form></section>
      <aside class="brief"><h2>撤销记录</h2><form id="billing-reverse-form">
        <label class="field">记录编号<input name="record_id" type="number" min="1" step="1" required /></label>
        <label class="field">撤销说明<input name="note" required maxlength="500" /></label>
        <div class="actions"><button class="secondary">撤销并补差</button></div></form>
        <p class="note">内容块清单为对象数组，每项含 address（SHA-256）与 bytes（实测字节）。填执行编号时从该次证据的块目录取实测字节。</p></aside></div>`:''}`;
}

export function bindBilling(state, api, update, fail) {
  const select = document.getElementById('billing-month');
  select?.addEventListener('change', ()=>{ state.billingMonth = select.value; void update(); });
  document.getElementById('billing-close')?.addEventListener('click', async()=>{
    const month = select?.value || state.billingMonth;
    if (!month) return fail(new Error('请先选择归属月份。'));
    try { await api(`/billing/periods/${month}/close`, {method:'POST'}); state.billingMonth = month; await update(); }
    catch (error) { fail(error); }
  });
  document.getElementById('billing-settle')?.addEventListener('click', async()=>{
    const month = select?.value || state.billingMonth;
    if (!month) return fail(new Error('请先选择归属月份。'));
    try { await api(`/billing/periods/${month}/settle`, {method:'POST'}); state.billingMonth = month; await update(); }
    catch (error) { fail(error); }
  });
  submit('billing-record-form', async data=>{
    const raw = data.get('blocks');
    let blocks;
    try {
      blocks = JSON.parse(raw || '[]');
      if (!Array.isArray(blocks)) throw new Error();
    } catch { throw new Error('内容块清单须为 JSON 数组。'); }
    const runId = String(data.get('run_id') || '').trim();
    const payload = {month:data.get('month'), production_group:data.get('production_group'),
      record_key:data.get('record_key'), note:String(data.get('note') || '').trim() || null};
    if (runId) payload.run_id = Number(runId);
    else payload.blocks = blocks;
    const row = await api('/billing/records', {method:'POST', data:payload});
    state.billingMonth = row.month;
    await update();
  }, fail);
  submit('billing-reverse-form', async data=>{
    const identity = Number(data.get('record_id'));
    const row = await api(`/billing/records/${identity}/reverse`, {method:'POST', data:{note:data.get('note')}});
    state.billingMonth = row.month;
    await update();
  }, fail);
}

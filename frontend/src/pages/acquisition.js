import{store}from'../stores/business.js';import{esc,num}from'../utils/format.js';import{table}from'../components/common/table.js';import{options}from'../components/common/fields.js';import{readonly}from'../components/common/notice.js';import{ACQUISITION_STATUS}from'../constants/enums.js';

const statusBadge=s=>`<span class=badge data-state=${s}>${ACQUISITION_STATUS[s]||s}</span>`;
const fmtTime=t=>t?String(t).slice(0,19).replace('T',' ')+' UTC':'—';

function spark(curve,selected){
  if(!curve||!curve.length)return '<span class=spark-empty>无曲线点</span>';
  const w=190,h=52,pad=3;
  const ts=curve.map(p=>p.t),fs=curve.map(p=>p.f);
  const t0=Math.min(...ts),t1=Math.max(...ts),f0=Math.min(0,...fs),f1=Math.max(...fs);
  const spanT=t1-t0||1,spanF=f1-f0||1;
  const x=t=>pad+(t-t0)/spanT*(w-2*pad),y=f=>h-pad-(f-f0)/spanF*(h-2*pad);
  const path=curve.map((p,i)=>(i?'L':'M')+x(p.t).toFixed(1)+' '+y(p.f).toFixed(1)).join(' ');
  const peak=curve.reduce((a,b)=>b.f>a.f?b:a,curve[0]);
  return `<svg class=spark viewBox="0 0 ${w} ${h}" role="img" aria-label="夹持曲线"><line x1=${pad} y1=${h-pad} x2=${w-pad} y2=${h-pad} class=spark-axis /><path d="${path}" class=spark-line ${selected?'data-selected=true':''}/><circle cx="${x(peak.t).toFixed(1)}" cy="${y(peak.f).toFixed(1)}" r=3 class=spark-peak/></svg>`;
}

function attemptGroups(d){
  const groups=new Map();
  for(const a of d.attempts||[]){if(!groups.has(a.specimen_code))groups.set(a.specimen_code,[]);groups.get(a.specimen_code).push(a);}
  const expected=new Set(d.expected_codes||[]);
  for(const code of expected)if(!groups.has(code))groups.set(code,[]);
  const locked=['sealed','pending_recovery','published'].includes(d.status);
  const canEdit=store.user.role!=='viewer'&&!locked;
  return [...groups.entries()].map(([code,list])=>{
    const rows=list.map(a=>`<tr class=${a.selected?'chosen':''}>
      <td>第 ${a.attempt_index} 次<small>${a.valid?'有效':'无效'}</small></td>
      <td>${num(a.open_t,3)} – ${num(a.close_t,3)}<small>${a.point_count} 点</small></td>
      <td><b>${a.max_force==null?'—':num(a.max_force,2)}</b><small>峰值 t=${a.peak_t==null?'—':num(a.peak_t,3)}</small></td>
      <td>${spark(a.curve,a.selected)}</td>
      <td>${a.selected?(a.selection_mode==='manual'?'人工选定':a.selection_mode==='fallback'?'回退选取':'自动选取'):'备选'}${a.selection_note?`<small>${esc(a.selection_note)}</small>`:''}</td>
      <td>${canEdit&&a.valid&&!a.selected?`<button class=text-btn data-attempt-select=${a.id} data-specimen=${esc(code)}>采用本次</button>`:''}</td>
    </tr>`).join('');
    const chosen=list.find(a=>a.selected);
    return `<section class=specimen-block>
      <div class=specimen-head><h3>${esc(code)}</h3><span>${list.length} 次夹持 · ${chosen?`入样峰值 ${num(chosen.max_force,2)} N`:'尚未选定入样夹持'}</span></div>
      ${list.length?`<div class=table-scroll><table><thead><tr><th>夹持</th><th>区间 s</th><th>最大拉力 N</th><th>曲线</th><th>依据</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p class=empty>尚未收到该试条的开闭夹事件与曲线。</p>'}
    </section>`;
  }).join('');
}

function batchDetail(d){
  if(!d)return '<section class=empty>选择或新建一个采集批，接收带序号的曲线分包后在此核查。</section>';
  const canEdit=store.user.role!=='viewer';
  const actions=d.actions||{};
  return `<section class=acq-detail>
    <div class=acq-head>
      <div><span class=eyebrow>采集批 #${d.id} · ${esc(d.lot)}</span><h2>${esc(d.code)}</h2>
        <p class=small>${d.packet_count} 个分包已收${d.declared_total?` / 共 ${d.declared_total} 包`:''} · 预期试条 ${(d.expected_codes||[]).length} 条 · 封批 ${fmtTime(d.sealed_at)}</p></div>
      <div class=acq-actions>${statusBadge(d.status)}
        ${canEdit&&actions.seal?'<button class=primary data-acq-seal>封批并校核</button>':''}
        ${canEdit&&actions.retry?'<button data-acq-retry>重试发布</button>':''}
        ${canEdit&&actions.reopen?'<button data-acq-reopen>撤销封批</button>':''}
        ${canEdit&&actions.receive?'<button data-acq-refresh>刷新校核</button>':''}
      </div>
    </div>
    ${d.status_reason?`<p class=notice>${esc(d.status_reason)}</p>`:''}
    ${(d.issues&&d.issues.length)&&d.status!=='published'?`<div class=issue-list><b>待处理问题</b><ul>${d.issues.map(i=>`<li>${esc(i)}</li>`).join('')}</ul></div>`:''}
    ${d.missing_seqs.length?`<p class=notice>缺包序号：${d.missing_seqs.join('、')}。隔离批可直接补传；已封批后收包会被拒绝，需先撤销封批。</p>`:''}
    <div class=acq-grid>
      <div>
        <div class=section-title><h2>夹持区间核查</h2><span>同一试条多次夹持保留全部依据，仅选定一次入样</span></div>
        ${canEdit?`<label class=inline-label>夹持选择依据（可选）<input id=selection-note placeholder="例如：首次夹具打滑，采用第二次完整夹持"></label>`:''}
        ${attemptGroups(d)}
      </div>
      <aside class=acq-aside>
        <div class=section-title><h2>分包收据</h2><span>${d.packet_count} 包</span></div>
        ${table(['序号','点 / 事件','摘要','接收'],(d.packets||[]).map(p=>[`#${p.seq}`, `${p.point_count} / ${p.event_count}`, `<small class=checksum>${esc(p.checksum.slice(0,12))}…</small>`, `<small>${esc(p.received_by)}${p.received_at?'<br>'+fmtTime(p.received_at):''}</small>`]))}
        <div class=section-title><h2>已发布样本</h2><span>${(d.samples||[]).length} 条</span></div>
        ${(d.samples||[]).length?table(['试条','拉力 N','标记'],d.samples.map(s=>[esc(s.code),num(s.force,2),s.flagged?'重夹 · 需复核':'正常'])):'<p class=empty>发布后样本才会进入方差分析。</p>'}
        ${d.archive_path?`<p class=small>曲线原件：<span class=checksum>${esc(d.archive_path)}</span><br>SHA-256：<span class=checksum>${esc(d.archive_sha256)}</span></p>`:''}
      </aside>
    </div>
  </section>`;
}

export function acquisitionPage(){
  const d=store.acquisition;
  const rows=store.acquisitions.map(b=>`<button class="history-row ${d&&d.id===b.id?'active':''}" data-acq-open=${b.id}>
    <span>#${b.id}　${esc(b.code)}<small>${esc(b.lot)} · ${b.packet_count} 包/${b.declared_total||'?'}</small></span>
    ${statusBadge(b.status)}<small>${b.issue_count?b.issue_count+' 项待处理':'无阻塞'}</small></button>`).join('');
  return `<header class=page-head><div><span class=eyebrow>03 / TENSILE ACQUISITION</span><h1>拉力曲线采集与核查</h1><p>按采集批接收带序号的曲线分包与开闭夹事件，跨包拼接夹持区间、提取峰值并核对预期试条。手工录入仍在拉力录入页保留。</p></div><span class=folio>${store.acquisitions.length}<small>采集批</small></span></header>
  <div class=acq-layout>
    <aside class=acq-list>
      <div class=section-title><h2>采集批</h2></div>
      ${rows||'<p class=empty>尚无采集批。</p>'}
      ${store.user.role==='viewer'?readonly():`<h2>新建采集批</h2><form id=acquisition-form>
        ${fieldText('采集批编号','code','HC-B-001')}
        <label>所属工艺<select name=process_id required>${options(store.processes.map(p=>[p.id,p.code+' / '+p.name]))}</select></label>
        ${fieldText('材料批次','lot','HC-1115')}
        <label>预期试条编号（逗号或换行分隔）<textarea name=expected_codes rows=3 required placeholder="HC-S1-036, HC-S1-037"></textarea></label>
        <label>备注<textarea name=note rows=2></textarea></label>
        <button class=primary>建立采集批</button></form>`}
    </aside>
    <div class=acq-main>
      ${d&&store.user.role!=='viewer'&&d.actions.receive?`<section class=packet-form>
        <div class=section-title><h2>接收曲线分包</h2><span>乱序可补；相同内容重传幂等</span></div>
        <form id=packet-form>
          <label>分包 JSON<textarea name=payload rows=8 required placeholder='{"seq":1,"declared_total":3,"curve":[{"t":0,"f":1.2}],"events":[{"type":"open","t":0,"specimen":"HC-S1-036"}]}'></textarea></label>
          <p class=small>字段：seq 分包序号、declared_total 总包数、curve[{t,f}]、events[{type: open/close, t, specimen}]。峰值可由同一次夹持跨多个分包拼接得到。</p>
          <button class=primary>接收并校核</button>
        </form>
      </section>`:''}
      ${batchDetail(d)}
    </div>
  </div>`;
}

const fieldText=(label,name,placeholder)=>`<label>${label}<input name=${name} type=text required placeholder="${esc(placeholder)}"></label>`;

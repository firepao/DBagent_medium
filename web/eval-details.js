(() => {
  const escape = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const call = async path => {
    const key = sessionStorage.getItem('adminKey') || '';
    const response = await fetch(path, {headers: key ? {'X-Admin-Key': key} : {}});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `请求失败 (${response.status})`);
    return response.json();
  };
  const runs = document.querySelector('#runs');
  const dialog = document.querySelector('#runDetailDialog');
  const addButtons = async () => {
    const data = await call('/api/v1/evaluations/runs').catch(() => []);
    document.querySelectorAll('.run').forEach(card => {
      if (card.querySelector('[data-run-detail]')) return;
      const text = card.querySelector('small')?.textContent || '';
      const run = data.find(item => text.includes(item.id));
      if (!run) return;
      const wrap = document.createElement('div'); wrap.className = 'run-actions';
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '查看逐题诊断'; button.dataset.runDetail = run.id;
      wrap.appendChild(button); card.appendChild(wrap);
    });
  };
  new MutationObserver(addButtons).observe(runs, {childList:true,subtree:true}); addButtons();
  document.querySelector('#closeRunDetail').onclick = () => dialog.close();
  runs.addEventListener('click', async event => {
    const button = event.target.closest('[data-run-detail]'); if (!button) return;
    dialog.showModal(); document.querySelector('#runDetailContent').innerHTML = '<p>正在加载…</p>';
    try {
      const run = await call(`/api/v1/evaluations/runs/${encodeURIComponent(button.dataset.runDetail)}`);
      document.querySelector('#runDetailTitle').textContent = `${run.target_id} · 逐题诊断`;
      document.querySelector('#runDetailMeta').textContent = `${run.id} · 行为通过 ${run.passed}/${run.total} · 黄金数值 ${run.value_accuracy == null ? '未标注' : `${(run.value_accuracy*100).toFixed(1)}%`} · 模型 ${run.model_calls||0} 次 · Token ${run.total_tokens==null?'未知':run.total_tokens}`;
      document.querySelector('#runDetailContent').innerHTML = run.results.map(item => `<article class="case-result ${item.passed?'passed':''}"><h3><span>${escape(item.case_id)}</span><span class="${item.passed?'ok':'bad'}">${item.passed?'通过':'失败'}</span></h3><div class="case-grid"><span>行为：${item.behavior_passed?'通过':'失败'} (${escape(item.actual_behavior)})</span><span>选表：${item.tables_passed?'通过':'失败'} (${escape((item.actual_tables||[]).join('、')||'无')})</span><span>数值：${item.values_passed?'通过':'失败'}</span></div>${item.error_code?`<p>错误码：${escape(item.error_code)}</p>`:''}${item.issues?.length?`<p class="bad">${item.issues.map(escape).join('；')}</p>`:''}<p>请求 ${escape(item.request_id)} · ${item.duration_ms} ms · 模型 ${item.model_calls||0} 次 · Token ${item.total_tokens==null?'未知':item.total_tokens}</p></article>`).join('') || '<p>本次运行没有逐题结果。</p>';
    } catch (error) { document.querySelector('#runDetailContent').innerHTML = `<p class="bad">${escape(error.message)}</p>`; }
  });
  document.querySelector('#compareDialog').addEventListener('change', async () => {
    const baseline = document.querySelector('#baselineRun').value, candidate = document.querySelector('#candidateRun').value;
    if (!baseline || baseline === candidate) return;
    try {
      const result = await call(`/api/v1/evaluations/compare?baseline_run_id=${encodeURIComponent(baseline)}&candidate_run_id=${encodeURIComponent(candidate)}`);
      await new Promise(resolve => setTimeout(resolve, 50));
      const important = result.changes.filter(item => ['fixed','regressed','still_failed','added','removed'].includes(item.change));
      const labels = {fixed:'已修复',regressed:'发生回归',still_failed:'持续失败',added:'新增',removed:'移除'};
      document.querySelector('#comparison').insertAdjacentHTML('beforeend', `<div class="comparison-cases"><strong>具体样本</strong>${important.length?`<ul>${important.map(item=>`<li class="change-${item.change}">${escape(item.case_id)} · ${labels[item.change]}</li>`).join('')}</ul>`:'<p>没有修复、回归或持续失败样本。</p>'}</div>`);
    } catch (_) {}
  });
})();

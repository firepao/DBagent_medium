(() => {
  const list = document.querySelector('#ruleList');
  const dialog = document.querySelector('#detailDialog');
  const escape = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const display = value => Array.isArray(value) ? value.join('、') : value && typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? '未填写');
  const call = async path => {
    const key = sessionStorage.getItem('adminKey') || '';
    const response = await fetch(path, {headers: key ? {'X-Admin-Key': key} : {}});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `请求失败 (${response.status})`);
    return response.json();
  };
  const addButtons = async () => {
    const allRules = await call('/api/v1/rules').catch(() => []);
    document.querySelectorAll('.rule').forEach(card => {
    const actions = card.querySelector('.actions');
    const source = actions?.querySelector('[data-id]');
    const versionText = [...(actions?.querySelectorAll('span') || [])].map(item => item.textContent).find(text => /^v\d+$/.test(text || ''));
    const title = card.querySelector('h2')?.textContent;
    const matched = allRules.find(rule => rule.payload.name === title && `v${rule.version}` === versionText);
    const ruleId = source?.dataset.id || matched?.id;
    if (!actions || !ruleId || actions.querySelector('[data-governance-detail]')) return;
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = '详情与差异';
    button.dataset.governanceDetail = ruleId;
    actions.prepend(button);
    });
  };
  new MutationObserver(addButtons).observe(list, {childList: true, subtree: true});
  addButtons();
  document.querySelector('#closeDetail').onclick = () => dialog.close();
  list.addEventListener('click', async event => {
    const button = event.target.closest('[data-governance-detail]');
    if (!button) return;
    event.stopImmediatePropagation();
    dialog.showModal();
    document.querySelector('#detailContent').innerHTML = '<p>正在加载…</p>';
    try {
      const id = encodeURIComponent(button.dataset.governanceDetail);
      const [rule, diff, audit] = await Promise.all([call(`/api/v1/rules/${id}`), call(`/api/v1/rules/${id}/diff`), call(`/api/v1/rules/${id}/audit`)]);
      document.querySelector('#detailTitle').textContent = `${rule.payload.name} · v${rule.version}`;
      document.querySelector('#detailSubtitle').textContent = `${rule.status} · ${rule.rule_key}`;
      const fields = [['业务说明',rule.payload.description],['业务对象',rule.payload.business_objects],['指标',rule.payload.metric],['维度',rule.payload.dimensions],['计算口径',rule.payload.calculation],['单位',rule.payload.unit],['数据表',rule.payload.scope_tables],['约束',rule.payload.constraints],['例外',rule.payload.exceptions],['正例',rule.payload.examples],['反例',rule.payload.counter_examples]];
      const changes = diff.changes.length ? diff.changes.map(item => `<div class="change"><strong>${escape(item.field)}</strong><del>之前：${escape(display(item.before))}</del><ins>当前：${escape(display(item.after))}</ins></div>`).join('') : '<p>与上一版本没有字段变化。</p>';
      const audits = audit.length ? `<ol class="audit-list">${audit.map(item => `<li>${escape(item.timestamp)} · ${escape(item.actor)} · ${escape(item.action)}</li>`).join('')}</ol>` : '<p>暂无审计记录。</p>';
      document.querySelector('#detailContent').innerHTML = `<h3>规则内容</h3><dl>${fields.map(([label,value]) => `<dt>${escape(label)}</dt><dd>${escape(display(value))}</dd>`).join('')}</dl><h3>版本差异 ${diff.from_version == null ? '（初始版本）' : `v${diff.from_version} → v${diff.to_version}`}</h3>${changes}<h3>审计历史</h3>${audits}`;
    } catch (error) {
      document.querySelector('#detailContent').innerHTML = `<p class="form-error">${escape(error.message)}</p>`;
    }
  }, true);
})();

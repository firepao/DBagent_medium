(() => {
  const form = document.querySelector('#ruleForm');
  const tableSelect = document.querySelector('#scopeTables');
  const options = document.querySelector('#requiredFieldOptions');
  const escape = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const split = value => String(value ?? '').split(/[，,；;]/).map(item => item.trim()).filter(Boolean);
  const request = async (path, options = {}, retried = false) => {
    const key = sessionStorage.getItem('adminKey') || '';
    const response = await fetch(path, {headers:{'Content-Type':'application/json',...(key?{'X-Admin-Key':key}:{})},...options});
    if (response.status === 401 && !retried) {
      const entered = prompt('请输入管理访问密钥');
      if (entered) { sessionStorage.setItem('adminKey', entered); return request(path, options, true); }
    }
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `请求失败 (${response.status})`);
    return response.json();
  };
  let catalog = [];
  const render = () => {
    const selected = new Set([...tableSelect.selectedOptions].map(item => item.value));
    options.innerHTML = catalog.filter(table => selected.has(table.name)).map(table => `<section class="field-table"><strong>${escape(table.dataset)} (${escape(table.name)})</strong><div class="field-options">${table.columns.map(column => `<label><input type="checkbox" data-required-table="${escape(table.name)}" value="${escape(column)}">${escape(column)}</label>`).join('')}</div></section>`).join('') || '<p>尚未选择数据表。</p>';
  };
  tableSelect.addEventListener('change', render);
  request('/api/v1/rules/catalog').then(data => { catalog = data.tables || []; render(); }).catch(error => { options.innerHTML = `<p class="field-error">${escape(error.message)}</p>`; });
  form.addEventListener('submit', async event => {
    event.preventDefault(); event.stopImmediatePropagation();
    const selectedTables = [...tableSelect.selectedOptions].map(item => item.value);
    const requiredFields = Object.fromEntries(selectedTables.map(table => [table, [...options.querySelectorAll(`[data-required-table="${CSS.escape(table)}"]:checked`)].map(item => item.value)]).filter(([,fields]) => fields.length));
    const error = document.querySelector('#formError');
    if (!Object.keys(requiredFields).length) { error.textContent = '请至少选择一个依赖字段，用于字段级校验。'; return; }
    const data = new FormData(form);
    const payload = {rule_key:data.get('rule_key'),name:data.get('name'),description:data.get('description'),business_objects:split(data.get('business_objects')),metric:data.get('metric'),dimensions:split(data.get('dimensions')),scope_tables:selectedTables,required_fields:requiredFields,calculation:data.get('calculation'),unit:data.get('unit'),constraints:split(data.get('constraints')),exceptions:[],examples:split(data.get('examples')),counter_examples:split(data.get('counter_examples'))};
    try { await request('/api/v1/rules',{method:'POST',body:JSON.stringify(payload)}); document.querySelector('#ruleDialog').close(); location.reload(); }
    catch (failure) { error.textContent = failure.message; }
  }, true);
})();

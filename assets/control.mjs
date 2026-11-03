export const STOP_WORKFLOW = 'https://github.com/modi-cjh/crypto-panel/actions/workflows/stop-demo.yml';

export function executionProfileText(data, now = Date.now()) {
  const p = data?.execution_profile;
  if (!p || !Number.isFinite(p.risk) || !Array.isArray(p.instruments)) return '策略档位：等待任务回执确认';
  const names = {aggressive:'激进模拟档',volume_test:'增量测试档',standard:'标准模拟档'};
  const name = names[p.mode] || '模拟策略';
  if (p.active && Number.isFinite(Date.parse(p.until)) && now >= Date.parse(p.until)) return `${name}窗口已结束，等待任务回执确认恢复标准档。`;
  const until = p.active && Number.isFinite(Date.parse(p.until))
    ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Bangkok',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(Date.parse(p.until)) : '';
  const count = p.scan_limit > p.instruments.length ? `${p.instruments.length}/${p.scan_limit} 币（等待全量扫描）` : `${p.instruments.length} 币`;
  return `${data.paused ? '已暂停 · ' : ''}${name} · 每笔止损风险 ${(p.risk*100).toFixed(0)}% · 同向上限 ${(p.same_side_cap*100).toFixed(0)}% · ${count}${until ? ` · ${until} 到期（UTC+7）` : ''}`;
}

export function stopControlText(data) {
  if (!data) return '全部平仓后保持暂停，需登录 GitHub 执行。';
  if (data.paused && data.stop_control?.status === 'failed') return '已暂停新开仓；平仓结果未全部确认，请检查执行回执。';
  if (data.paused && data.stop_control?.status === 'completed') return '程序持仓已全部平仓，交易已停止。';
  if (data.paused && data.stop_control?.status === 'pending') return '已暂停新开仓，正在等待平仓回执。';
  if (data.paused) return '当前已暂停新开仓，仍可执行全部平仓。';
  return '全部平仓后保持暂停，需登录 GitHub 执行。';
}

export function renderTradeControls(data) {
  document.querySelectorAll('[data-stop-status]').forEach(el => { el.textContent = stopControlText(data); });
  document.querySelectorAll('[data-execution-profile]').forEach(el => { el.textContent = executionProfileText(data); });
}

export function initTradeControls() {
  const dialog = document.getElementById('stop-dialog');
  document.querySelectorAll('[data-stop-demo]').forEach(el => {
    el.addEventListener('click', () => dialog.showModal());
  });
  document.getElementById('stop-dialog-close').addEventListener('click', () => dialog.close());
  document.getElementById('stop-workflow-link').href = STOP_WORKFLOW;
  dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
}

export const STOP_WORKFLOW = 'https://github.com/modi-cjh/crypto-panel/actions/workflows/stop-demo.yml';

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

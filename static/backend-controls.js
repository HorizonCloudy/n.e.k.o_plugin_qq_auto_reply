/* Shared controls for the saved, locally managed OneBot backend. */
function renderBackendControls(el, settings, rt, selected, call, refresh) {
  if (!el) return;
  el.hidden = settings.qq_connection_mode === 'open_platform';
  if (el.hidden) return;
  const backend = selected || settings.onebot_backend || 'napcat';
  const label = onebotBackend(backend).label;
  const pending = backend !== (settings.onebot_backend || 'napcat');
  const alive = !!rt.napcat_running, managed = alive && !!rt.napcat_managed;
  if (!el.querySelector('button')) {
    el.innerHTML = '<strong></strong><p class="backend-process-state"></p><div class="backend-process-actions"><button type="button" class="btn btn-primary backend-start"></button><button type="button" class="btn btn-ghost ghost backend-stop"></button></div><p class="backend-process-hint"></p><p class="backend-process-result" role="status"></p>';
  }
  el.querySelector('strong').textContent = label + ' 运行控制';
  el.querySelector('.backend-process-state').textContent = pending ? '请先保存后端选择' :
    alive ? '进程运行中' + (rt.napcat_pid ? ' · PID ' + rt.napcat_pid : '') + (rt.onebot_connected ? ' · OneBot 已连接' : ' · 等待 OneBot 连接') :
    rt.onebot_connected ? 'OneBot 已连接 · 外部管理' : '无插件管理的进程 · OneBot 未连接';
  el.querySelector('.backend-process-hint').textContent = '启动后端后，可单独启动自动回复。关闭后端会同时停止自动回复。' +
    (backend === 'snowluma' ? '关闭 SnowLuma 不会退出本机 QQ。' : '关闭 NapCat 会退出它启动的 QQ。') +
    '手动或远程启动的后端需在对应控制台关闭。';
  const start = el.querySelector('.backend-start'), stop = el.querySelector('.backend-stop');
  start.textContent = '启动 ' + label;
  stop.textContent = '关闭 ' + label;
  start.disabled = !!el._busy || pending || alive || !!rt.onebot_connected;
  stop.disabled = !!el._busy || pending || !managed;
  async function run(action) {
    if (el._busy) return;
    el._busy = true;
    start.disabled = stop.disabled = true;
    const result = el.querySelector('.backend-process-result');
    result.textContent = '正在' + (action === 'backend_start' ? '启动 ' : '关闭 ') + label + '…';
    try {
      await call('runtime', {action, backend});
      result.textContent = label + (action === 'backend_start' ? ' 已启动；自动回复需单独启动。' : ' 已关闭，自动回复已停止。');
    } catch (error) { result.textContent = '操作失败：' + error.message; }
    finally { el._busy = false; await refresh(); }
  }
  start.onclick = () => run('backend_start');
  stop.onclick = () => run('backend_stop');
}

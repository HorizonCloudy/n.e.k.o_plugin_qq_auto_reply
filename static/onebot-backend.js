/* Backend-specific UI copy and setting names. Wire direction is independent. */
window.OneBotBackends = Object.freeze({
  napcat: Object.freeze({
    label: 'NapCat', directoryKey: 'napcat_directory', windowKey: 'show_napcat_window',
    directoryHint: '输入绝对路径。留空则使用存档目录中的 NapCat.Shell。',
    loginHint: 'NapCat 启动后自动获取二维码，请使用填写的机器人 QQ 号扫码。',
    loginMethod: 'qrcode',
  }),
  snowluma: Object.freeze({
    label: 'SnowLuma', directoryKey: 'snowluma_directory', windowKey: 'show_snowluma_window',
    directoryHint: '输入 SnowLuma 根目录（包含 index.mjs）。留空则使用存档目录中的 SnowLuma。',
    loginHint: 'SnowLuma 接入已运行的本机 QQ。首次先打开控制台设置管理密码，再在 QQ 窗口登录机器人账号；本页自动确认 OneBot 连接。',
    loginMethod: 'native_qq',
    guides: {
      'step-napcat': '<p>在一键部署页选择 SnowLuma，填写机器人 QQ 号后部署。完整包自带 Node，程序和配置存放在存档目录。</p><p>请安装并打开本机 QQ，登录填写的账号。SnowLuma 会接入已运行的 QQ；如未接入，在 SnowLuma WebUI 检查 QQ 进程和 Hook 状态。点击控制台按钮会自动带入可用的初始密码；修改密码后使用新密码登录。QQ 二维码请在本机 QQ 窗口扫描。</p>',
      'step-onebot': '<p>SnowLuma 配置位于 config/onebot_账号.json，网络字段使用 networks.wsClients（反向）和 networks.wsServers（正向）。一键部署会合并写入连接配置。</p><p>正向地址请使用根路径 /，role 为 Universal；/api 只处理动作、/event 只接收事件。两端 Token 必须一致。</p>',
      'step-linux': '<p>Linux 一键部署支持 x64 / arm64，安装包内置 Node。需自行安装并登录 Linux QQ，并具备 SnowLuma 接入该 QQ 进程的权限。Docker 或远程部署请按 SnowLuma 官方文档配置，再填写 OneBot 地址。</p>',
    },
  }),
});
window.onebotBackend = key => window.OneBotBackends[key] || window.OneBotBackends.napcat;

window.renderSnowLumaSetup = (element, visible) => {
  element.style.display = visible ? '' : 'none';
  if (!visible || element.innerHTML) return;
  element.innerHTML = `<ol class="snowluma-steps">
    <li><b>初始化控制台</b><p>点击「SnowLuma 控制台」，首次按提示确认协议并设置自己的管理密码。密码由 SnowLuma 保存，之后使用新密码登录。</p></li>
    <li><b>打开并登录桌面 QQ</b><p>在运行 SnowLuma 的电脑上打开官方 QQ，登录一键部署时填写的机器人账号。二维码在 QQ 窗口中显示，本页不提供二维码刷新。</p></li>
    <li><b>确认 QQ 进程已接入</b><p>SnowLuma 会检测 QQ 进程并自动加载 Hook。一键部署已开启自动加载；若未连接，在控制台查看目标 QQ 进程与 Hook 状态。多个 QQ 同时运行时，请核对目标账号。</p></li>
    <li><b>确认 OneBot 连接</b><p>一键部署已写入通信配置。QQ 登录并接入后，本页会自动更新账号与连接状态。仅打开 QQ 或控制台不代表已连接。</p></li>
  </ol><div class="snowluma-note">控制台管理密码、QQ 登录、OneBot Token 是三件独立的事。QQ 与 SnowLuma 应使用同一个系统用户、相同权限运行；远程部署请在服务器上操作 QQ。</div>`;
};

// Resolve a login link only when clicked; keep passwords out of dashboard DOM.
window.updateBackendConsoleLink = (link, key, url, pending, call) => {
  const label = onebotBackend(key).label;
  link.style.display = '';
  link.textContent = label + ' 控制台';
  link.title = pending ? '请先保存后端选择，再打开控制台' : !url
    ? '请先完成部署并刷新状态，确认后端目录正确'
    : '在新标签页打开 ' + label + '；自动带入可用的初始密码';
  link.setAttribute('aria-disabled', pending || !url ? 'true' : 'false');
  link.removeAttribute('href');
  if (!pending && url) link.href = url;
  link.onclick = async event => {
    event.preventDefault();
    if (pending || !url) return;
    let shell = window.electronShell;
    try { shell = shell || window.parent.electronShell; } catch (_) { /* Cross-origin frame. */ }
    const external = shell && typeof shell.openExternal === 'function';
    const embedded = external || /Electron/i.test(window.navigator?.userAgent || '');
    // Electron owns popup windows: a returned WindowProxy may be null or detached.
    // Only a normal browser needs a synchronously reserved tab for popup policy.
    const tab = embedded ? null : window.open('about:blank', '_blank');
    if (!embedded && !tab) { link.title = '请允许此页面打开新标签页后重试'; return; }
    if (tab) tab.opener = null;
    let destination = url, timer;
    try {
      const result = await Promise.race([
        call('query', {action:'napcat_webui'}),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), 8000); }),
      ]);
      if (result.backend && result.backend !== key) {
        if (tab) tab.close();
        link.title = '后端已切换，请刷新页面后再打开';
        return;
      }
      destination = result.url || url;
    } catch (_) { /* Query unavailable: open the normal manual-login URL. */ }
    finally { clearTimeout(timer); }
    try {
      const parsed = new URL(destination);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('invalid protocol');
      if (external) await shell.openExternal(parsed.href);
      else if (embedded) window.open(parsed.href, '_blank', 'noopener,noreferrer');
      else if (!tab.closed) tab.location.replace(parsed.href);
    } catch (_) {
      if (tab) tab.close();
      link.title = '打开失败，请在浏览器手动打开控制台地址';
    }
  };
};

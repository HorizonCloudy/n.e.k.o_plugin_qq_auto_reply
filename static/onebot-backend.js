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
    loginHint: '请在本机 QQ 登录填写的机器人账号，二维码在本机 QQ 窗口中显示；控制台按钮可自动带入有效的初始密码。登录后页面会自动确认连接状态。',
    loginMethod: 'native_qq',
    guides: {
      'step-napcat': '<p>在一键部署页选择 SnowLuma，填写机器人 QQ 号后部署。完整包自带 Node，程序和配置存放在存档目录。</p><p>请安装并打开本机 QQ，登录填写的账号。SnowLuma 会接入已运行的 QQ；如未接入，在 SnowLuma WebUI 检查 QQ 进程和 Hook 状态。点击控制台按钮会自动带入可用的初始密码；修改密码后使用新密码登录。QQ 二维码请在本机 QQ 窗口扫描。</p>',
      'step-onebot': '<p>SnowLuma 配置位于 config/onebot_账号.json，网络字段使用 networks.wsClients（反向）和 networks.wsServers（正向）。一键部署会合并写入连接配置。</p><p>正向地址请使用根路径 /，role 为 Universal；/api 只处理动作、/event 只接收事件。两端 Token 必须一致。</p>',
      'step-linux': '<p>Linux 一键部署支持 x64 / arm64，安装包内置 Node。需自行安装并登录 Linux QQ，并具备 SnowLuma 接入该 QQ 进程的权限。Docker 或远程部署请按 SnowLuma 官方文档配置，再填写 OneBot 地址。</p>',
    },
  }),
});
window.onebotBackend = key => window.OneBotBackends[key] || window.OneBotBackends.napcat;

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
    const tab = window.open('about:blank', '_blank');
    if (!tab) return;
    tab.opener = null;
    try {
      const result = await call('query', {action:'napcat_webui'});
      if (result.backend && result.backend !== key) {
        tab.close();
        link.title = '后端已切换，请刷新页面后再打开';
        return;
      }
      tab.location.replace(result.url || url);
    } catch (_) {
      tab.location.replace(url); // Still allow manual login if lookup failed.
    }
  };
};

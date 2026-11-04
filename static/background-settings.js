/* Background preferences are local to this browser profile and plugin UI origin. */
(() => {
  'use strict';
  const key = 'qq_auto_reply_background_v1';
  let busy = false;
  function apply(value) {
    if (value && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(value)) {
      document.body.style.backgroundImage = 'linear-gradient(rgba(255,255,255,.3),rgba(255,255,255,.3)),url("' + value + '")';
    } else {
      document.body.style.removeProperty('background-image');
    }
  }
  try { apply(localStorage.getItem(key)); } catch (_) { /* Default background remains usable. */ }
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
  const panel = document.querySelector('[data-background-settings]');
  if (!panel) return;
  panel.innerHTML = '<div class="card-title">背景图片</div><p style="font-size:13px;color:var(--text-secondary);margin:12px 0">选择本地 JPG、PNG 或 WebP 图片（最大 10 MB），自动缩放后应用到插件各页面。图片仅保存在当前浏览器／本体中；清除本地数据后需重新选择。</p><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><label class="btn btn-ghost">选择图片<input type="file" accept="image/jpeg,image/png,image/webp" style="display:none"></label><button type="button" class="btn btn-ghost">恢复默认</button></div><p role="status" style="font-size:13px;margin-top:12px"></p>';
  const input = panel.querySelector('input');
  const reset = panel.querySelector('button');
  const status = panel.querySelector('[role="status"]');
  function lock(value) { busy = value; input.disabled = reset.disabled = value; }
  input.addEventListener('change', async () => {
    const file = input.files[0];
    if (!file || busy) return;
    lock(true);
    status.textContent = '正在处理图片…';
    let url;
    try {
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw Error('请选择 JPG、PNG 或 WebP 图片');
      if (file.size > 10 * 1024 * 1024) throw Error('图片不能超过 10 MB');
      url = URL.createObjectURL(file);
      const img = new Image();
      await new Promise((resolve, reject) => {
        img.onload = resolve;
        img.onerror = () => reject(Error('图片无法读取，请换一张图片'));
        img.src = url;
      });
      const scale = Math.min(1, 1920 / Math.max(img.naturalWidth, img.naturalHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
      const context = canvas.getContext('2d');
      context.fillStyle = '#ffffff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(img, 0, 0, canvas.width, canvas.height);
      const value = canvas.toDataURL('image/jpeg', .82);
      if (value.length > 3 * 1024 * 1024) throw Error('图片处理后仍过大，请选择更小的图片');
      try { localStorage.setItem(key, value); }
      catch (_) { throw Error('本地存储不可用或空间不足，背景未保存'); }
      apply(value);
      status.textContent = '背景已保存，插件各页面共用。';
    } catch (error) { status.textContent = error.message; }
    finally { if (url) URL.revokeObjectURL(url); input.value = ''; lock(false); }
  });
  reset.addEventListener('click', () => {
    try {
      localStorage.removeItem(key);
      apply(null);
      status.textContent = '已恢复各页面的默认背景。';
    } catch (_) { status.textContent = '本地存储不可用，无法清除已保存的背景。'; }
  });
})();

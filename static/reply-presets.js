/* The server owns preset values. This controller only previews unsaved choices. */
const QQReplyPresets = (() => {
    let metadata = null, mode = 'balanced', values = {}, customDraft = null;
    const descriptions = {
        fast: '关闭群聊和私聊缓冲，关闭自动休眠与注意力消耗/回落。仍遵守回复资格和安全检查，不保证每条消息都回复。',
        balanced: '使用现有默认回复节奏：适度合并消息，按原有注意力和休眠规则参与聊天。',
        quiet: '更长的消息合并窗口、更低的发言频率软提示；注意力回落更慢，群静默休眠延长至 2 小时。',
        custom: '保留你的当前参数，可在下方细调。切换预设再切回自定义，会恢复本次未保存的自定义编辑。',
    };
    const element = id => document.getElementById(id);
    const clone = value => JSON.parse(JSON.stringify(value));
    function show() {
        element('cfg-reply-preset').value = mode;
        const description = element('reply-preset-description');
        const key = 'ui.reply_preset.' + mode + '.description';
        description.setAttribute('data-i18n', key);
        description.textContent = typeof I18n !== 'undefined' ? I18n.t(key, descriptions[mode]) : descriptions[mode];
        document.querySelectorAll('[data-reply-custom-only]').forEach(el => {
            el.hidden = mode !== 'custom';
        });
    }
    function writeValues() {
        Object.entries(metadata.controls).forEach(([key, control]) => {
            const el = element(control.id);
            if (!el) return;
            if (control.kind === 'bool') el.checked = !!values[key];
            else el.value = control.kind === 'dict' ? JSON.stringify(values[key], null, 2) : values[key];
        });
    }
    function captureControls() {
        const raw = {};
        Object.values(metadata.controls).forEach(control => {
            const el = element(control.id);
            if (el) raw[control.id] = control.kind === 'bool' ? el.checked : el.value;
        });
        return raw;
    }
    return {
        configure(data) { if (data && data.values && data.controls) metadata = data; },
        // Called by applySettings only after its dirty-form guard.
        load(settings) {
            if (!metadata) return;
            mode = settings.reply_behavior_preset || 'custom';
            if (!Object.hasOwn(descriptions, mode)) mode = 'custom';
            values = clone(metadata.values.balanced);
            Object.keys(values).forEach(key => {
                if (settings[key] !== undefined) values[key] = clone(settings[key]);
            });
            customDraft = null;
            writeValues();
            element('cfg-reply-preset').disabled = false;
            show();
        },
        select(next) {
            if (!metadata || !Object.hasOwn(descriptions, next) || next === mode) return;
            if (mode === 'custom') customDraft = {values: clone(values), raw: captureControls()};
            mode = next;
            if (mode === 'custom' && customDraft) {
                values = clone(customDraft.values);
                Object.values(metadata.controls).forEach(control => {
                    const el = element(control.id);
                    if (el && Object.hasOwn(customDraft.raw, control.id)) {
                        el[control.kind === 'bool' ? 'checked' : 'value'] = customDraft.raw[control.id];
                    }
                });
            } else if (mode !== 'custom') {
                values = clone(metadata.values[mode]);
                writeValues();
            }
            show();
        },
        isCustom() { return !metadata || mode === 'custom'; },
        payload(args) {
            if (!metadata) return args;
            const result = {...args, reply_behavior_preset: mode};
            Object.keys(values).forEach(key => {
                if (mode !== 'custom') delete result[key];
                // Knobs with no input still need to survive preset -> custom.
                else if (!Object.hasOwn(result, key)) result[key] = clone(values[key]);
            });
            return result;
        },
    };
})();

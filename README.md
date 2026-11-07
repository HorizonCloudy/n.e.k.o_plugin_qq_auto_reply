# QQ集成 (qq_auto_reply)

通过 OneBot v11（正向/反向 WebSocket，兼容 SnowLuma / NapCat / LLOneBot / go-cqhttp / Lagrange 等任意 OneBot 后端）或 QQ 官方开放平台接入 QQ 的完整机器人集成插件。

- **双通道接入**：OneBot v11 泛用连接（正向/反向）+ QQ 官方开放平台
- **多群注意力管理**：群间注意力竞争、焦点切换、回溯补回
- **接话判定（谁在跟谁说话）**：@你 / 引用你 / 引用别人 / @别人 / 叫你的名字 / 她刚说完的
  第一条 —— 明确在跟别人说话时不抢话，判据同时进打分、门控与提示词
- **动态回复缓冲**：多条缓冲汇总、发送门控自缓冲、频率软提示
- **多模态输出**：文本、语音、图片、表情包、戳一戳、键盘
- **文件内容读取**：文本 / VLM 图片描述
- **提示词编辑**：运行时动态修改系统提示词与场景模板

## 私聊与群聊的记忆方向

- **私聊 → 群聊**：核心记忆与 `recall_memory` 可只读当前真实发言人的私聊记忆。
  管理员使用原有主人记忆库；普通/受信任用户使用自己的 `participant` 记忆域。
- **群聊记忆 → 私聊不开放**：私聊不检索群或群成员记忆。
  存储仍独立，群聊结算不会因为只读共享而写入私聊域。
- **活跃会话信息保留**：开启 `allow_cross_group_context` 时，私聊仍可注入群号、对象称呼、权限等会话元数据；
  这不包含群聊天记录或长期记忆，关闭开关后不再注入。
- 沿用授权开关：须开启 `group_memory_enabled`、`group_member_memory_enabled`；
  非管理员另须开启 `private_participant_memory_enabled`。接收消息后才开启，不追溯授权该轮。
- 不读取旁观者、最近其他发言人的私聊记忆；合并、主动发言、异步任务回投等合成轮次不借用名义发送者身份。
  权限或记忆开关在读取、生成或投递途中撤销时，相关内容/草稿会被丢弃。
- 这是后续读取路径的隔离，不迁移或删除既有记录；用户主动发送/转发到私聊的内容仍属于该私聊已收到的消息。

## 插件信息

| 字段 | 值 |
| --- | --- |
| 插件 ID | `qq_auto_reply` |
| 类型 | `plugin` |
| 版本 | `0.14.11-beta.1` |
| SDK | `>=0.1.0,<0.3.0` |
| 被动模式 | 是 |

## 实验功能：插件授权联动的只读背景

开发代码支持将“她的一天”的桌面陪伴事实和“永远的陪伴”的角色状态，
作为 DeepChat 私聊情绪的辅助背景。**默认关闭，仅绑定主人的管理员 OneBot 私聊**；
不回写情绪、不新增主动消息。“永远的陪伴”需安装单独只读适配。
完成一次主人绑定后，来源跟随现有插件授权：加入管理员区即接入已适配能力，撤销即停用。
不再额外维护两套来源开关；自动背景适配器可扩展，普通插件仍走原工具桥。
配置、迁移、兼容性与测试步骤见 [只读陪伴增强](docs/companion-context.md)。
本功能自 `0.14.6-beta.7` 测试版提供，默认关闭。

## 开发与验证

```bash
uv run neko-plugin check qq_auto_reply
uv run neko-plugin build qq_auto_reply
```

## 配置

运行时配置（注意力阈值、回溯参数、群信任列表等）由 `business_config.json` 提供，位于 N.E.K.O 数据根目录 `data/plugins/qq_auto_reply/` 下。

- 群名单 `trusted_groups` 里的群**默认就是信任群**（走注意力门控正常参与）：条目漏写
  `level`、写空、写错级别都按信任群收录。想让她只在被 @ 时按
  `normal_relay_probability`（默认 0.1）低概率转发给主人，必须**显式**写
  `"level": "normal"`。

### 积压管理与桌面提醒

QQ 集成保留群消息积压记录、摘要/详情查看、标记已审和群内回溯处理，
但不再自动把积压摘要推送给桌面猫娘，也不做静默上下文注入。
桌面重要消息提醒由 QQ 助手负责；普通群原有的 QQ 转发逻辑不受影响。
旧配置中的 `backlog_notify_cooldown_seconds` 和 `backlog_issue_notify_threshold`
仅为兼容而保留，不再生效；无需删除已有积压数据。

### 按群开启表情包收集

在 OneBot／开放平台管理页的群列表中，每个群都有 **允许自动收集表情包** 开关。
新群及没有配置该字段的已有群均默认关闭；只加入信任群并不会自动同意存图。
开启后仍受全局 `sticker_collect_policy` 约束，`any` 也不能绕过群开关。
关闭后，该群的表情包不会通过收集工具或图库自动收图入口新增保存；已有库存不删除，
私聊、普通图片和手动上传不受这个群开关影响。开关开启须保存成功才生效。

配置存放于 `trusted_groups` 的对应条目，例如：

```json
{"group_id": "123456", "level": "trusted", "sticker_collect_enabled": true}
```

省略或设为 `false` 即关闭；只接受 JSON 布尔值，不把字符串 `"true"` 当成开启。
修改群级别或转发概率不会重置已有的收集开关。

### OneBot 一键部署后端

一键部署页选择 **OneBot** 后，可选择 **NapCat / SnowLuma**；选择 **QQ 开放平台** 时隐藏后端选择和部署区域。旧配置默认继续使用 NapCat，正向／反向 WebSocket 的选择独立于部署后端。

- 两套程序分别存放在插件持久化数据目录的 `NapCat.Shell/` 和 `SnowLuma/`，各自保留安装路径和窗口设置。切换后端先停止旧连接和插件管理的进程，再手动部署／启动所选后端。
- NapCat 沿用二维码登录和 `config/onebot11_<QQ号>.json`；SnowLuma 写入 `config/onebot_<QQ号>.json`，通过本机 QQ 登录，页面提供 SnowLuma WebUI 入口并根据实时 OneBot 连接确认在线状态，不展示旧的 NapCat 二维码。
- SnowLuma 一键部署固定使用官方 **v2.4.0 完整包**（自带 Node），支持 Windows x64、Linux x64／arm64；安装前校验发布包 SHA-256 和大小，修复程序文件时保留配置及运行数据。需预先安装本机 QQ，Linux 还需具备接入 QQ 进程的权限。
- SnowLuma 首次 WebUI 登录凭据可查看其 `logs/neko-launch.log`。配置页的目录、启动选项、登录提示和引导内容会随后端切换。

配置依据：[SnowLuma 配置参考](https://snowluma.github.io/zh/docs/guide/configuration)、[v2.4.0 官方发布](https://github.com/SnowLuma/SnowLuma/releases/tag/v2.4.0)。

### SnowLuma 手动连接注意（以下根路径行为曾在 v1.14.20 真机验证）

- 正向 WebSocket 请连**根路径**（如 `ws://127.0.0.1:3002/`）：收事件 + 调动作。
  SnowLuma 的 `wsServers` 在 `path: "/"` + `role: "Universal"` 时另有按路径分流的
  语义——`/api` **只能调动作、不推送消息事件**（连上但收不到消息），`/event` 只收事件。
  token 两种携带方式（`?access_token=` 与 `Authorization: Bearer`）都支持。
- `trusted_users` / `trusted_groups` 条目是对象（`{"qq": "123", "level": "trusted"}`）。
  写成字符串数组也能启动（按默认档收录），但面板编辑建议用对象格式。

### 图库与表情包统一管理

- 自动收图、保存表情包和手动上传共用 `sticker.json` 与 `sticker/`；在原有的**表情包管理页**查看图片、删除和管理描述，不再维护第二份图库索引。图库工具、头像工具读取同一份库存，删除后立即不可见。
- 普通聊天图片只作为素材管理，不自动出现在可发送表情目录；通过 `save_sticker` 收藏后才能作为表情使用。私聊/群聊来源作用域保留，群表情收集仍默认关闭。
- 升级首次启动自动迁移旧 `gallery/index.json`，每条迁移成功后记录进度，完成后归档为 `gallery/index.migrated.json`。旧 `gallery/blobs/` 只作为恢复备份保留，不再作为活动库存，也不会因重启把已删除图片重新导入。来源无法确定的旧收图限制为不可跨会话发送。
- 自动图库收集到达上限后停止新增，不再淘汰用户维护的库存。请在表情包管理页清理；升级不会主动删除旧图片。

import io, subprocess

msg = """feat(qq): 官方机器人面板接入注意力与休眠调节（v0.15.12）

使用者报"官方平台接入群聊时 LLM 不回复"。查下来是两件事叠在一起：

1. `attention_*` / `dormancy_*` 这些设置键本来就是两条通道共用的，控件却只画在
   napcat.html 里。官方机器人开了全量群消息之后要走注意力闸，界面上一个旋钮都
   没有，只能任由"不回"发生。现在官方面板有同一套 8 个注意力控件 + 3 个休眠控件，
   并沿用 NapCat 那条修正：情绪倍率表 JSON 不合法就报错并中止整次保存（不静默丢键
   还谎报已保存）。
2. 真正的头号的静默原因是群 id 命名空间不同：这条通道的群 id 是十六进制
   group_openid，而信任群名单里留的是 QQ 群号（真机日志实证：
   `[Gate] 群C26BCC94C516E6E6DDC3399D75FC8676 不参与注意力竞争，放行给下游`，
   名单里只有 1065604240 / 953698696）。对不上就退化成普通群、只按概率转达。
   修的是可见性与取数：`open_platform action=groups_seen` 列出真的收到过消息的群
   并直接标出未进名单的那些；门控日志每群提醒一次并给出修法。**没有**自动加信任 ——
   那是主人的授权决定。
3. 顺带修一个识别缺口：机器人自己的群内 member_openid 只有白名单接口 bot_state 给，
   没权限时全量事件里的"@ 她"会被判成没 @ 她；现在从"只 @ 了一个人"的群 @ 事件里学一次。

已知局限：groups_seen 是内存计数，插件重启后要先收到一条群消息才有内容（界面文案已按此写）。

测试：+7 项（面板契约对齐两套面板、休眠三项、groups_seen 与未信任清单、门控提示只说一次、
member_openid 学习）；真宿主下全套 2997 项通过；ruff 通过；neko-plugin check 0 error。
"""
subprocess.run(["git", "add", "-A"], check=True)
r = subprocess.run(["git", "commit", "-q", "-F", "-"], input=msg, text=True, encoding="utf-8", capture_output=True)
print("commit", r.returncode, r.stderr[:200])
print(subprocess.run(["git", "log", "--oneline", "-3"], capture_output=True, text=True).stdout)

token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
url = f"https://x-access-token:{token}@github.com/HorizonCloudy/n.e.k.o_plugin_qq_auto_reply.git"
p = subprocess.run(["git", "-c", "credential.helper=", "push", url,
                    "feat/qq-open-platform-api-v2:feat/qq-open-platform-api-v2"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print("push rc", p.returncode, (p.stderr or p.stdout).replace(token, "***")[-300:])

body = """补一条：使用者随后报了"官方平台群聊里 LLM 不回"，同一条分支上又多了一个提交（`0.15.12`）。

要点两条，跟本 issue 上面的内容连在一起看：

1. 官方机器人面板原来一个注意力旋钮都没有（`attention_*` / `dormancy_*` 这些键两条通道共用，控件只画在 NapCat 面板）。现在两套面板同一组控件，契约测试也扩到两个面板（`test_qq_attention_panel_surface.py`）。
2. 真机查到的静默主因是**群 id 命名空间不同**：这条通道的群 id 是十六进制 `group_openid`，而信任群名单里留的是 QQ 群号，永远匹配不上 → 那个群退化成普通群、只按概率转达。日志实证：`[Gate] 群C26BCC94C516E6E6DDC3399D75FC8676 不参与注意力竞争，放行给下游`。修法给的是可见性（`open_platform action=groups_seen` 列出真的见过消息的群并标出未进名单的 + 每群一次的门控提示），**没有自动加信任**——那是主人的授权决定。

验证：全套 2997 项通过（+7），`ruff` 通过，`neko-plugin check` 0 error，本机已装到运行中的宿主并只读复验面板与 `groups_seen`。
"""
c = subprocess.run(["gh", "issue", "comment", "11", "--repo",
                    "zhaijiunknow/n.e.k.o_plugin_qq_auto_reply", "--body", body],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print("comment rc", c.returncode, (c.stdout or c.stderr)[:160])

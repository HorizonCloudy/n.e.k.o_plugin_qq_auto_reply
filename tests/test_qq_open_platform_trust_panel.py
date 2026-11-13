# -*- coding: utf-8 -*-
"""官方机器人面板的信任群入口。

为什么单独一个契约测试：`open_platform.html` 里有 `renderGroups()` 这个函数，
**但页面里根本没有那张表**，`applyDashboard()` 也从来没调用它 —— 于是"信任群聊"
在官方机器人面板上整个不存在（没表、没添加按钮、没编辑）。使用者 2026-11-13 的原话是
"我没看到这个设置"。这类缺陷不会让任何断言变红：函数还在、代码看着没问题，
只是渲染目标不在。所以这里钉的是**界面本身**。
"""

from __future__ import annotations

import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
HTML = (PLUGIN / "static" / "open_platform.html").read_text(encoding="utf-8")


def test_trusted_group_table_actually_exists_in_the_markup():
    assert 'id="group-table"' in HTML, "信任群聊的表又只剩 JS 里的引用了"


def test_apply_dashboard_renders_the_group_roster():
    body = HTML.split("function applyDashboard", 1)[1].split("function ", 1)[0]
    assert "renderGroups()" in body, "applyDashboard 不渲染群名单 = 面板上永远看不到它"


def test_group_add_and_edit_are_reachable_from_the_panel():
    assert "showModal('group')" in HTML, "没有添加信任群的入口"
    assert "function showGroupModal" in HTML, "群弹窗实现不见了"
    assert "action:'group_add'" in HTML, "弹窗没走 trust 入口"
    # 级别与普通转发概率必须能改（NapCat 面板一直有这两项）
    assert 'id=\"mg-level\"' in HTML or "mg-level" in HTML
    assert "mg-nrp" in HTML


def test_renderers_tolerate_a_missing_container():
    """表缺失时要安静返回，而不是抛异常打断整次 applyDashboard。

    真机上这个函数一旦抛，面板上**后面**的区块（用户名单、ID 认领）也一起不渲染 ——
    症状是"半个面板不见了"，比少一个表难查得多。
    """
    for fn in ("renderUsers", "renderGroups"):
        head = HTML.split(f"function {fn}(", 1)[1][:200]
        assert "if(!tb)return" in head, f"{fn} 没有容器缺失的保护"


def test_seen_groups_can_promote_a_group_in_one_click():
    """群 id 是十六进制 openid，手抄必错 —— 必须能从「我看过的群」直接加。"""
    assert "loadSeenGroups()" in HTML
    assert "unclaimed" in HTML, "看过但没进名单的群要能标出来"
    assert "ui.openplat.seen.add" in HTML
    assert "groups_seen" in HTML, "没调用 open_platform 的 groups_seen 动作"

# -*- coding: utf-8 -*-
"""许可与来源声明的一致性。

这类东西坏掉的方式很安静：`LICENSE` 换文本、`pyproject` 字段与它不一致、
或者把"待上游确认"这句话写丢了 —— 读者就会以为整份项目已经重新授权。
所以把三处口径钉在一起。
"""

from __future__ import annotations

import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
LICENSE = (PLUGIN / "LICENSE").read_text(encoding="utf-8")
NOTICE = (PLUGIN / "NOTICE").read_text(encoding="utf-8")
README = (PLUGIN / "README.md").read_text(encoding="utf-8")
PYPROJECT = (PLUGIN / "pyproject.toml").read_text(encoding="utf-8")


def test_license_is_the_verbatim_gpl3_text():
    flat = " ".join(LICENSE.split())
    assert flat.startswith("GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007")
    assert "either version 3 of the License, or (at your option) any later version" in flat


def test_pyproject_license_matches_the_file():
    assert 'license = "GPL-3.0-only"' in PYPROJECT


def test_notice_states_the_scope_and_the_pending_consent():
    assert "GPL-3.0-only" in NOTICE
    assert "只覆盖" in NOTICE or "仅及于" in NOTICE
    assert "必须先取得" in NOTICE, "「要挂到上游必须先拿到作者同意」这句话不能丢"


def test_provenance_covers_every_external_source():
    for needle in ("Project-N-E-K-O/N.E.K.O", "MaiBot", "Apache-2.0", "zhaijiunknow"):
        assert needle in README, f"README 的来源表少了 {needle}"
    assert "没有复制任何代码" in NOTICE or "未并入" in README


def test_apache_text_and_host_notice_are_kept():
    apache = (PLUGIN / "LICENSES" / "Apache-2.0.txt").read_text(encoding="utf-8")
    assert "Apache License" in apache and "Version 2.0" in apache
    assert "Project N.E.K.O" in NOTICE, "宿主 NOTICE 原文照录丢了"


def test_vendored_files_declare_their_origin():
    """带宿主 Apache-2.0 头的测试文件必须仍有头，且改动过的副本仍有 LOCAL-PATCH。"""
    for name in ("tests/test_qq_open_platform_actor_identity.py",
                 "tests/test_qq_open_platform_identity_scope.py"):
        head = (PLUGIN / name).read_text(encoding="utf-8", errors="replace")[:400]
        assert "Project N.E.K.O. Team" in head and "Apache License" in head
    vendor_head = (PLUGIN / "_vendor" / "connection_onebot" / "qq_open_platform_media.py").read_text(
        encoding="utf-8", errors="replace"
    )[:700]
    assert "LOCAL-PATCH" in vendor_head, "改过的连接器副本必须继续标出本地改动"

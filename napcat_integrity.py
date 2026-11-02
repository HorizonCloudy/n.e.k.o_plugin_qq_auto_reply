"""Installed-file checks; mutable user data is deliberately outside the baseline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import napcat_platform

MANIFEST = ".qq-auto-reply-integrity.json"

# 安装根目录下由启动器重新生成的文件；不等同于需要重装时保留的用户数据。
# launcher*.bat 会按当前安装路径覆盖 loadNapCat.js，缺失/为空也能自行生成。
RUNTIME_GENERATED_FILES = frozenset({"loadnapcat.js"})


def is_runtime_generated_file(name: str) -> bool:
    return Path(name.replace("\\", "/")).as_posix().casefold() in RUNTIME_GENERATED_FILES


def is_program_file(name: str) -> bool:
    path = Path(name)
    return (
        path.parts[0].lower() not in {"config", "logs", "cache", "plugins"}
        and path.name != MANIFEST
        and not path.name.lower().endswith("-user.bat")
        and path.name.lower() != "qqnt.json"
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_manifest(root: Path, names: list[str]) -> None:
    """Called only on freshly extracted files, never baseline an existing install."""
    files = {
        name: {"size": (root / name).stat().st_size, "sha256": digest(root / name)}
        for name in names if is_program_file(name) and not is_runtime_generated_file(name)
    }
    (root / MANIFEST).write_text(
        json.dumps({"schema": 1, "files": files}, ensure_ascii=False), encoding="utf-8",
    )


def check_installation(root: Path, *, launcher: Path | None = None) -> tuple[bool, str]:
    """Hash managed installs; report the limited scope for untracked installs."""
    root = Path(root).resolve()
    launcher = launcher or napcat_platform.find_launcher(root)
    required = ["napcat.mjs", "package.json"]
    if napcat_platform.is_windows():
        required += ["loadNapCat.js", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll"]
    issues = []
    try:
        if launcher is None or not launcher.is_file() or launcher.stat().st_size == 0:
            issues.append("启动器缺失或为空")
        for name in required:
            if is_runtime_generated_file(name):
                continue
            path = root / name
            if not path.is_file() or path.stat().st_size == 0:
                issues.append(f"缺失或为空: {name}")
        for name in ("native", "node_modules"):
            path = root / name
            if not path.is_dir() or not any(p.is_file() for p in path.rglob("*")):
                issues.append(f"依赖目录缺失或为空: {name}")
        manifest = root / MANIFEST
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            files = data["files"]
            if data.get("schema") != 1 or not isinstance(files, dict) or not files:
                raise ValueError("无效的文件清单")
            checked_files = 0
            for name, expected in files.items():
                path = (root / name).resolve()
                if not path.is_relative_to(root) or path == root:
                    raise ValueError("清单路径超出安装目录")
                # 旧版安装清单也必须应用豁免，无需用户删除或重建清单。
                if is_runtime_generated_file(name):
                    continue
                checked_files += 1
                if not path.is_file():
                    issues.append(f"文件缺失: {name}")
                elif path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
                    issues.append(f"文件损坏或被修改: {name}")
            success = f"完整性校验通过（{checked_files} 个程序文件）"
        else:
            success = "关键文件检查通过；此安装无文件清单，未完成哈希校验"
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        issues.append(f"完整性检查失败: {exc}")
    if issues:
        detail = "；".join(issues[:10])
        if len(issues) > 10:
            detail += f"；另有 {len(issues) - 10} 项"
        return False, detail
    return True, success

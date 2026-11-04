"""Pinned SnowLuma full packages, verified before installing program files."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx

from .napcat_install import safe_member_path, sha256_of

VERSION = "v2.4.0"
ASSETS = {
    "win-x64": ("zip", 43339979, "93096cb6d4cf576999094e9f7d1c9fb08e22f6b41c879182f015e639dc437613"),
    "linux-x64": ("tar.gz", 52916249, "1f936d488445e4803d4316c7704924854d7722805969af63975bb37b67222926"),
    "linux-arm64": ("tar.gz", 52526068, "75311338de0d6588295ae71d4a242584294a0b5b349b35af2145eddaad6b10b8"),
}
MANIFEST = ".neko-snowluma-files.json"
PRESERVED = {"config", "logs", "data", "cache"}
MIRROR_DELAY = 2.0
DOWNLOAD_TIMEOUT = 240


def platform_key():
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    key = f"{'win' if os.name == 'nt' else platform.system().lower()}-{arch}"
    if key not in ASSETS:
        raise RuntimeError("SnowLuma 一键部署支持 Windows x64 / Linux x64、arm64")
    return key


async def download(directory: Path, progress, report) -> Path:
    key = platform_key()
    extension, size, digest = ASSETS[key]
    name = f"SnowLuma-{VERSION}-{key}.{extension}"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.is_file() and target.stat().st_size == size and await asyncio.to_thread(sha256_of, target) == digest:
        return target
    url = f"https://github.com/SnowLuma/SnowLuma/releases/download/{VERSION}/{name}"
    # A slow but live GitHub stream must not block the mirror for four minutes.
    # Separate partial files let both sources run; only a verified winner is used.
    parts = [target.with_name(name + f".{source}.part") for source in ("official", "mirror")]
    high_water = 0

    async def fetch(index, prefix):
        nonlocal high_water
        if index:
            await asyncio.sleep(MIRROR_DELAY)
        label = "GitHub 直连" if not prefix else "镜像加速"
        report(f"开始{label}下载，采用最先通过官方 SHA-256 校验的安装包")
        part = parts[index]
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                async with client.stream("GET", prefix + url) as response:
                    response.raise_for_status()
                    done = 0
                    hasher = hashlib.sha256()
                    with part.open("wb") as output:
                        async for chunk in response.aiter_bytes(65536):
                            done += len(chunk)
                            if done > size:
                                raise RuntimeError("安装包大小超出官方记录")
                            output.write(chunk)
                            hasher.update(chunk)
                            if done > high_water:
                                high_water = done
                                progress(done, size)
                if done != size or hasher.hexdigest() != digest:
                    raise RuntimeError("SnowLuma 安装包大小或 SHA-256 校验失败")
                return part, label
        except (httpx.HTTPError, OSError, RuntimeError, TimeoutError) as exc:
            report(f"{label}未完成（{type(exc).__name__}），等待其他下载源")
            return None

    tasks = [asyncio.create_task(fetch(i, prefix))
             for i, prefix in enumerate(("", "https://gh-proxy.com/"))]
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            for task in asyncio.as_completed(tasks):
                result = await task
                if result is not None:
                    part, label = result
                    part.replace(target)
                    report(f"{label}下载完成，官方 SHA-256 校验通过")
                    return target
    except TimeoutError:
        report("下载超时，请检查网络后重试")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for part in parts:
            part.unlink(missing_ok=True)
    raise RuntimeError("SnowLuma 下载失败，请检查网络后重试")


def check_installation(root: Path) -> tuple[bool, str]:
    key = platform_key()
    native = "win32-x64" if key == "win-x64" else key
    suffix = "dll" if key == "win-x64" else "so"
    required = ["index.mjs", "check-node-version.cjs", "node.exe" if key == "win-x64" else "node",
                f"native/snowluma-{native}.node", f"native/snowluma-{native}.{suffix}"]
    for name in required:
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            return False, f"缺失或空文件: {name}"
    manifest = root / MANIFEST
    if manifest.is_file():
        try:
            files = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(files, dict) or not files:
                return False, "程序校验清单无效"
            for name, digest in files.items():
                path = safe_member_path(name, root)
                if path is None or not path.is_file() or sha256_of(path) != digest:
                    return False, f"程序文件校验失败: {name}"
        except (ValueError, OSError):
            return False, "无法读取程序校验清单"
    return True, "SnowLuma 程序完整性检查通过"


def install(archive: Path, target: Path):
    """Stage and validate first; keep all existing runtime data and configuration."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sl-stage-", dir=target.parent) as temporary:
        stage = Path(temporary)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for entry in bundle.infolist():
                    path = safe_member_path(entry.filename.rstrip("/"), stage)
                    if path is None or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise RuntimeError("SnowLuma 安装包包含不安全路径")
                    if entry.is_dir():
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(entry) as src, path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(archive) as bundle:
                for entry in bundle:
                    if entry.isdir() and entry.name in (".", "./"):
                        continue
                    path = safe_member_path(entry.name.rstrip("/"), stage)
                    if path is None or not (entry.isfile() or entry.isdir()):
                        raise RuntimeError("SnowLuma 安装包包含不安全路径或链接")
                    if entry.isdir():
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(entry) as src, path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    path.chmod(entry.mode & 0o777)
        roots = [stage] + [p.parent for p in stage.glob("*/index.mjs")]
        root = next((p for p in roots if (p / "index.mjs").is_file()), stage)
        ok, detail = check_installation(root)
        if not ok:
            raise RuntimeError(detail)
        hashes = {}
        for src in root.rglob("*"):
            if not src.is_file():
                continue
            relative = src.relative_to(root)
            if relative.parts[0] in PRESERVED or relative.name == MANIFEST:
                continue
            dst = safe_member_path(relative.as_posix(), target)
            if dst is None:
                raise RuntimeError("安装目标包含指向目录外的路径")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            hashes[relative.as_posix()] = sha256_of(src)
        (target / MANIFEST).write_text(json.dumps(hashes), encoding="utf-8")

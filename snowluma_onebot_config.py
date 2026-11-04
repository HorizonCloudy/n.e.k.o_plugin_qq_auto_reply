"""SnowLuma uses onebot_<uin>.json and networks.wsClients/wsServers."""
import json
import shutil
import time
from pathlib import Path


def read_object(path: Path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象: {path.name}")
    return value


def write_object(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".neko.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _remove_stock_listeners(config):
    networks = config.setdefault("networks", {})
    for kind, name, port in (("httpServers", "http-default", 3000), ("wsServers", "ws-default", 3001)):
        rows = networks.get(kind, [])
        networks[kind] = [r for r in rows if not (
            r.get("name") == name and r.get("host") == "127.0.0.1"
            and r.get("port") == port and r.get("path", "/") == "/"
        )]


def apply(root: Path, uin: str, *, reverse_url: str, forward_host: str, forward_port: int, token: str,
          forward_path="/", mode="napcat"):
    if not uin.isascii() or not uin.isdigit() or uin.startswith("0"):
        raise ValueError("请填写有效的机器人 QQ 号")
    path = root / "config" / f"onebot_{uin}.json"
    if mode not in ("napcat", "napcat_forward"):
        raise ValueError("SnowLuma 仅支持 OneBot 正向或反向连接")
    # Read every affected file before writing. An explicit empty global config
    # prevents SnowLuma from creating stock listeners for newly discovered QQs.
    global_path = root / "config" / "onebot.json"
    paths = {global_path, path, *root.glob("config/onebot_[0-9]*.json")}
    configs = {p: read_object(p) for p in paths}
    config = configs[path]
    for value in configs.values():
        _remove_stock_listeners(value)
    networks = config.setdefault("networks", {})
    selected = "wsServers" if mode == "napcat_forward" else "wsClients"
    for kind, extra in (
        ("wsClients", {"url": reverse_url, "reconnectIntervalMs": 5000}),
        ("wsServers", {"host": forward_host, "port": forward_port, "path": forward_path}),
    ):
        name = f"neko-qq-{kind}"
        rows = networks.setdefault(kind, [])
        row = next((r for r in rows if r.get("name") == name), None)
        if kind != selected:
            if row is not None:
                row["enabled"] = False
            continue
        if row is None:
            # Reuse an existing endpoint instead of starting a second listener
            # on the same port or sending duplicate reverse-WS events.
            row = next((r for r in rows if (
                r.get("url") == reverse_url if kind == "wsClients" else
                r.get("port") == forward_port
                and r.get("host", "0.0.0.0") in (forward_host, "0.0.0.0", "::")
            )), None)
        if row is None:
            row = {"name": name}
            rows.append(row)
        row.update(name=name, enabled=True, role="Universal", accessToken=token, messageFormat="array", reportSelfMessage=False, **extra)
    runtime_path = root / "config" / "runtime.json"
    runtime = read_object(runtime_path)
    runtime["hookAutoLoad"] = True
    runtime.setdefault("webuiHost", "127.0.0.1")
    runtime.setdefault("webuiPort", 5099)
    configs[runtime_path] = runtime
    for destination, value in configs.items():
        if destination.exists():
            if read_object(destination) == value:
                continue
            shutil.copy2(destination, destination.with_name(destination.name + f".neko-backup-{time.time_ns()}"))
        write_object(destination, value)
    return path

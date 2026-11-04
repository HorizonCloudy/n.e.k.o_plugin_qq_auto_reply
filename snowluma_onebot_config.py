"""SnowLuma uses onebot_<uin>.json and networks.wsClients/wsServers."""
import json
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


def apply(root: Path, uin: str, *, reverse_url: str, forward_host: str, forward_port: int, token: str, forward_path="/"):
    if not uin.isascii() or not uin.isdigit() or uin.startswith("0"):
        raise ValueError("请填写有效的机器人 QQ 号")
    path = root / "config" / f"onebot_{uin}.json"
    config = read_object(path) if path.exists() else read_object(root / "config" / "onebot.json")
    networks = config.setdefault("networks", {})
    for kind, extra in (
        ("wsClients", {"url": reverse_url, "reconnectIntervalMs": 5000}),
        ("wsServers", {"host": forward_host, "port": forward_port, "path": forward_path}),
    ):
        name = f"neko-qq-{kind}"
        rows = networks.setdefault(kind, [])
        row = next((r for r in rows if r.get("name") == name), None)
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
        row.update(enabled=True, role="Universal", accessToken=token, messageFormat="array", reportSelfMessage=False, **extra)
    write_object(path, config)
    runtime_path = root / "config" / "runtime.json"
    runtime = read_object(runtime_path)
    runtime["hookAutoLoad"] = True
    runtime.setdefault("webuiHost", "127.0.0.1")
    runtime.setdefault("webuiPort", 5099)
    write_object(runtime_path, runtime)
    return path

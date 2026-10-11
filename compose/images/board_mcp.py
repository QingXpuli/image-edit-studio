"""Board MCP server(stdio)。

让 agent(如 ZCode)通过 MCP 工具在 /board 上"备料":放文本/图片节点、连线、跑 dry-run 预算。
刻意不提供真实生成工具——真实提交只能由人在页面上点「生成」
(ui_policy: explicit_board_submit,AGENTS.md 不变量 2/4)。

协议:JSON-RPC 2.0 over stdio,换行分隔(MCP stdio transport),零第三方依赖。
写入走 board_app.save_board 的 mtime 乐观锁,避免与网页端双头覆盖。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import board_app  # noqa: E402

SERVER_INFO = {"name": "mantu-board", "version": "0.1.0"}


# ---- helpers ----

def _read_state(bid: str):
    board = board_app.load_board(bid)
    p = board_app._board_path(bid)
    mt = p.stat().st_mtime if p.is_file() else 0.0
    return board, mt


def _auto_pos(board: dict) -> dict:
    """放在最右侧节点的右边,避免叠住现有内容。"""
    xs = [float(o.get("x", 0)) + float(o.get("w") or 300)
          for o in board.get("objects", []) if o.get("type") != "rect"]
    return {"x": (max(xs) + 60) if xs else 80, "y": 80}


def _with_lock(bid: str, mutate) -> dict:
    """每轮重读→应用变更→mtime 乐观锁保存;冲突则重试(新增型操作可安全重放)。"""
    for _ in range(3):
        board, mt = _read_state(bid)
        mutate(board)
        r = board_app.save_board(board, bid, expected_mtime=mt)
        if not r.get("conflict"):
            return r
    return {"ok": False, "message": "画布写入冲突重试 3 次仍失败,请稍后重读画布再试。", "conflict": True}


def _obj_view(o: dict) -> dict:
    keep = ["id", "type", "x", "y", "w", "h", "text", "prompt", "kind", "channel",
            "size", "pad", "input_fidelity", "style_scope", "pose_scope",
            "group", "parent", "name", "path"]
    return {k: o[k] for k in keep if k in o}


def _text_result(payload, error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
        "isError": bool(error),
    }


# ---- tool implementations ----

def tool_board_list(_args: dict) -> dict:
    return {"ok": True, "boards": board_app.list_boards()}


def tool_board_read(args: dict) -> dict:
    bid = str(args.get("bid") or "default")
    board, _mt = _read_state(bid)
    ident, ref, prompt, gen = board_app.resolve_roles(board)
    return {
        "ok": True,
        "bid": bid,
        "name": board.get("name") or bid,
        "objects": [_obj_view(o) for o in board.get("objects", []) if o.get("type") != "rect"],
        "edges": board.get("edges", []),
        "roles": {
            "identity": ident.get("id") if ident else "",
            "reference": ref.get("id") if ref else "",
            "gen": gen.get("id") if gen else "",
            "dock_prompt": prompt,
        },
        "size": board.get("size"), "pad": board.get("pad"),
    }


def tool_board_add_text(args: dict) -> dict:
    bid = str(args.get("bid") or "default")
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "message": "text 不能为空。"}
    node = {
        "id": uuid.uuid4().hex[:10], "type": "text", "text": text,
        "x": float(args["x"]) if "x" in args else None,
        "y": float(args["y"]) if "y" in args else None,
    }

    def apply(b):
        pos = _auto_pos(b)
        node["x"] = node["x"] if node["x"] is not None else pos["x"]
        node["y"] = node["y"] if node["y"] is not None else pos["y"]
        b["objects"].append(dict(node))

    r = _with_lock(bid, apply)
    if r.get("conflict"):
        return r
    return {"ok": True, "bid": bid, "node_id": node["id"], "message": f"已放置文本节点 {node['id']}"}


def tool_board_add_image(args: dict) -> dict:
    bid = str(args.get("bid") or "default")
    path = str(args.get("path") or "")
    p = board_app.allowed_path(path)
    if p is None:
        return {"ok": False, "message": "路径不在白名单目录(产图/图生图用/t1/style-distill/compose/.zimage)或不是图片文件。"}
    from PIL import Image
    with Image.open(p) as im:
        w, h = im.size
    scale = 280 / max(1, w)
    node = {
        "id": uuid.uuid4().hex[:10], "type": "image",
        "path": str(p), "name": p.name,
        "x": float(args["x"]) if "x" in args else None,
        "y": float(args["y"]) if "y" in args else None,
        "w": w * scale, "h": h * scale,
    }

    def apply(b):
        pos = _auto_pos(b)
        node["x"] = node["x"] if node["x"] is not None else pos["x"]
        node["y"] = node["y"] if node["y"] is not None else pos["y"]
        b["objects"].append(dict(node))

    r = _with_lock(bid, apply)
    if r.get("conflict"):
        return r
    return {"ok": True, "bid": bid, "node_id": node["id"],
            "message": f"已放置图片节点 {node['id']}({p.name})"}


def tool_board_wire(args: dict) -> dict:
    bid = str(args.get("bid") or "default")
    src = str(args.get("from") or "")
    dst = str(args.get("to") or "")
    probe, _mt = _read_state(bid)
    a = next((o for o in probe["objects"] if o["id"] == src), None)
    b = next((o for o in probe["objects"] if o["id"] == dst), None)
    if not a or not b:
        return {"ok": False, "message": f"节点不存在: {'from ' + src if not a else ''} {'to ' + dst if not b else ''}".strip()}
    if src == dst:
        return {"ok": False, "message": "不能连接自身。"}

    def apply(bb):
        if any(e["from"] == src and e["to"] == dst for e in bb["edges"]):
            return False  # 已存在,幂等跳过
        bb["edges"].append({"id": uuid.uuid4().hex[:10], "from": src, "to": dst})
        if b.get("type") == "gen":
            bb["genId"] = dst
        return True

    saved = {"ok": False}
    board, mt = _read_state(bid)
    if not apply(board):
        return {"ok": True, "bid": bid, "message": "连线已存在,跳过。"}
    for _ in range(3):
        saved = board_app.save_board(board, bid, expected_mtime=mt)
        if not saved.get("conflict"):
            break
        board, mt = _read_state(bid)
        if not apply(board):
            return {"ok": True, "bid": bid, "message": "连线已存在,跳过。"}
    if saved.get("conflict"):
        return saved
    return {"ok": True, "bid": bid,
            "message": f"已连线 {a.get('type')}→{b.get('type')}" + ("(图一/图二按接线自动解析)" if b.get("type") == "gen" else "")}


def tool_board_budget(args: dict) -> dict:
    bid = str(args.get("bid") or "default")
    board, _mt = _read_state(bid)
    r = board_app.run_edit(board, dry_run=True)
    r["bid"] = bid
    return r


TOOLS = [
    {
        "name": "board_list",
        "description": "列出所有画布(id、名称、节点数、更新时间)。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "board_read",
        "description": "读取画布:节点、连线、图一/图二/生成节点角色、底栏提示词。",
        "inputSchema": {
            "type": "object",
            "properties": {"bid": {"type": "string", "description": "画布 id,默认 default"}},
            "required": [],
        },
    },
    {
        "name": "board_add_text",
        "description": "在画布上新增一个文本节点(agent 的分析/提示词落到画布)。省略坐标时自动放在最右侧。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bid": {"type": "string"}, "text": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "board_add_image",
        "description": "把白名单目录(产图/图生图用/t1/style-distill/compose/.zimage)内的图片作为图片节点放上画布。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bid": {"type": "string"}, "path": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "board_wire",
        "description": "连接两个节点(from→to)。连到生成节点即参与图一/图二/文本角色解析。",
        "inputSchema": {
            "type": "object",
            "properties": {"bid": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "string"}},
            "required": ["from", "to"],
        },
    },
    {
        "name": "board_budget",
        "description": "对当前画布跑 dry-run 预算(本地校验接线/尺寸/白名单,不请求上游、不产图)。真实生成必须由人在页面上点「生成」。",
        "inputSchema": {
            "type": "object",
            "properties": {"bid": {"type": "string"}},
            "required": [],
        },
    },
]

TOOL_FUNCS = {
    "board_list": tool_board_list,
    "board_read": tool_board_read,
    "board_add_text": tool_board_add_text,
    "board_add_image": tool_board_add_image,
    "board_wire": tool_board_wire,
    "board_budget": tool_board_budget,
}


def call_tool(name: str, args: dict) -> dict:
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return _text_result({"ok": False, "message": f"unknown tool: {name}"}, error=True)
    try:
        return _text_result(fn(args))
    except Exception as e:  # noqa: BLE001
        return _text_result({"ok": False, "message": f"{type(e).__name__}: {e}"}, error=True)


# ---- stdio JSON-RPC loop ----

def _resp(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(msg: dict):
    method = msg.get("method")
    if "id" not in msg:
        return None  # notification:不回应
    try:
        if method == "initialize":
            params = msg.get("params") or {}
            return _resp(msg["id"], {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return _resp(msg["id"], {})
        if method == "tools/list":
            return _resp(msg["id"], {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            return _resp(msg["id"], call_tool(str(params.get("name") or ""), params.get("arguments") or {}))
        return _err(msg["id"], -32601, f"unknown method: {method}")
    except Exception as e:  # noqa: BLE001
        return _err(msg["id"], -32603, f"{type(e).__name__}: {e}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        out = handle(msg)
        if out is None:
            continue
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

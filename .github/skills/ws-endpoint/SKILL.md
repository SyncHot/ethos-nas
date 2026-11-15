---
name: ws-endpoint
description: "Add or change a /ws/* endpoint. Use for terminal/logs/stats/file-events sockets, WS auth, close codes 4001/4002/4003/1013, allowlists, cleanup, and connectWs."
argument-hint: "What does the socket stream, and who may connect?"
---

# WebSocket endpoint

## Contract (from `docs/api-conventions.md`)
- URL: `ws(s)://host/ws/<name>?token=<jwt>`; the JWT is mandatory.
- Messages are JSON objects `{"type": ..., ...}`; the terminal PTY is the only binary stream.
- Close codes are part of the API:

| Code | Meaning |
|---|---|
| 4001 | missing/invalid token |
| 4002 | invalid target (not allowlisted) |
| 4003 | authenticated but role not allowed |
| 1013 | capacity / too many sessions |
| 1011 | internal failure |
| 1000 | normal completion |

Never invent new codes; clients stop reconnecting permanently on 4001/4003.

## Procedure

### 1. Backend handler
```python
logger = logging.getLogger("ethos-nas.routers.<name>")

@router.websocket("/ws/<name>")
async def stream(websocket: WebSocket):
    token = websocket.query_params.get("token") or websocket.cookies.get("token")
    user = _verify(token)                      # copy the auth approach from terminal.py / logs.py
    if not user:
        await websocket.close(code=4001, reason="Authentication required")
        return
    await websocket.accept()
    try:
        ...
    except WebSocketDisconnect:
        logger.info("client disconnected from /ws/<name>")
    except Exception:
        logger.error("WS /ws/<name> failed", exc_info=True)
        await websocket.close(code=1011)
    finally:
        cleanup()                              # subscriptions, PTY, task_registry entries
```

### 2. Authorization and targets
- Admin-only streams: check `user["role"]` and close with **4003**.
- Any user-supplied target (container name, log name, PTY target) must pass an allowlist or
  regex validation; reject with **4002**. See `terminal.py::_validate_target`.
- Cap concurrent sessions per user/process and close with **1013** when exceeded.
- Subprocess/PTY work: argument lists only, no `shell=True`; clean up in `finally`.

### 3. Mount
Add the router in `backend/main.py` if new. WS routes bypass the HTTP `public_paths` check, so each handler validates its own token.

### 4. Frontend
Use the hardened helper, never a raw socket:
```js
import { connectWs } from '../core/ws.js';

const ctl = connectWs('/ws/<name>', (msg) => paint(msg), {
    appId: '<app>',            // auto-close when the window closes
    heartbeatMs: 25000,
    stopOnCodes: [4002],       // permanent failures for this endpoint
    onAuthFailure: () => {},   // 4001/4003 — do not retry
});
ctl.send({ subscribe: '...' });
ctl.close();
```
`connectWs` provides exponential backoff + jitter, stops on auth failures, and returns a controller
(not the raw socket), so the caller can close the live connection after reconnects.

### 5. Verify
On an isolated instance: no token → **4001**, user token on an admin stream → **4003**, bad target → **4002**, capacity → **1013**; the client backs off and stops on auth expiry; no leaked PTY after closing. Run `tests/test_ws_contract.py` and `scripts/check_logging.py`.

## Done when
Token is validated before `accept`, codes match the table, targets are allowlisted, cleanup runs in
`finally`, the frontend uses `connectWs` with `appId`, and the contract test passes.

# HTTP Tunnel Mapping Redesign

## Context

Current tunnel functionality uses TCP port mapping: requests to `/tunnel/{session_id}/{port}/{path}` are proxied to client's `local_host:local_port`. This requires knowing the session_id and port in the URL, which is cumbersome. The user wants simpler HTTP URL-based tunnels where each tunnel gets a unique auto-generated ID, and requests to `/tunnel/{tunnel_id}/{path}` forward to the client's local service.

## Changes

### 1. `revpty/server/tunnel.py` - Data model & manager

**TunnelMapping dataclass** - replace `remote_port` with `tunnel_id`:
```python
@dataclass
class TunnelMapping:
    id: str               # Same as tunnel_id
    tunnel_id: str        # Auto-generated short ID (8-char hex)
    session_id: str
    local_host: str       # Target host on client (default 127.0.0.1)
    local_port: int       # Target service port on client
    created_at: float
```

**TunnelManager methods**:
- `add_mapping(session_id, local_port, local_host="127.0.0.1")` - auto-generate `tunnel_id` via `uuid.uuid4().hex[:8]`, use it as mapping `id`
- `get_mapping_by_tunnel_id(tunnel_id)` - new lookup by tunnel_id (replaces `get_mapping(session_id, port)`)
- `remove_mapping(tunnel_id)` - unchanged semantically
- `list_mappings(session_id)` - unchanged
- Add `import uuid` at top

Note: `tunnel.py` was partially edited before plan mode. The TunnelMapping dataclass change needs to be completed/corrected.

### 2. `revpty/server/app.py` - Routes & handlers

**Routes** (line ~574-578):
```
# Old:
/tunnel/{session_id}/{port}/{path:.*}
/tunnel/{session_id}/{port}

# New:
/tunnel/{tunnel_id}/{path:.*}
/tunnel/{tunnel_id}
```

**`tunnel_api_handler`** (POST, line ~174-190):
- Accept: `session_id`, `local_port`, `local_host` (optional, default 127.0.0.1)
- Remove: `remote_port` parameter
- Call: `tunnel_manager.add_mapping(session_id, local_port, local_host)`
- Return: `{id, tunnel_id, local_host, local_port, url}` where `url` = `/tunnel/{tunnel_id}`

**`tunnel_api_handler`** (GET, line ~165-172):
- Return `tunnel_id`, `local_host`, `local_port` (remove `remote_port`)

**`tunnel_proxy_handler`** (line ~206-248):
- Get `tunnel_id` from `request.match_info["tunnel_id"]` instead of `session_id`+`port`
- Look up mapping via `tunnel_manager.get_mapping_by_tunnel_id(tunnel_id)`
- Get `session_id` from `mapping.session_id` instead of URL
- Rest of proxy logic unchanged

**`tunnel_delete_handler`** (line ~193-203):
- `mapping_id` is now `tunnel_id` - no change needed in logic

### 3. `revpty/client/tunnel_proxy.py` - No changes

The client proxy already receives `local_host` and `local_port` in the tunnel_request payload. No modifications required.

### 4. `revpty/client/agent.py` - No changes

The agent just forwards the payload to `TunnelProxy.handle_request()`. No changes needed.

### 5. Frontend `revpty/server/static/index.html` - Tunnel modal form

Replace tunnel form inputs (line ~84-89):
```html
<!-- Old: Remote Port, Local Host, Local Port -->
<!-- New: Local Port, Local Host (optional) -->
<input type="number" id="tunnel-local-port" placeholder="Service Port" />
<input type="text" id="tunnel-local-host" placeholder="127.0.0.1" value="127.0.0.1" />
<button class="btn-sm" id="btn-add-tunnel">Add</button>
```

Update modal title: "TCP Tunnels" -> "HTTP Tunnels"

### 6. Frontend `revpty/server/static/app.js` - Tunnel UI logic

**Variable references** (line ~823-826):
- Remove `tunnelRemotePort` reference
- Keep `tunnelLocalHost`, `tunnelLocalPort`

**`renderTunnelList()`** (line ~856-877):
- Show: tunnel URL `/tunnel/{tunnel_id}` instead of `:remote_port -> local_host:local_port`
- Display the full URL as a clickable link

**`btnAddTunnel` handler** (line ~879-901):
- POST body: `{ session_id, local_port, local_host }` (remove `remote_port`)
- Validate `local_port` is required instead of `remote_port`

### 7. `tests/test_new_features.py` - Update tests (line ~160-216)

Update `TunnelManagerTests`:
- `test_add_and_get_mapping`: call `add_mapping("s1", 3000)`, verify `tunnel_id` exists, use `get_mapping_by_tunnel_id()`
- `test_remove_mapping`: use auto-generated `tunnel_id` from `add_mapping` return value
- `test_list_mappings_filter`: update `add_mapping` calls (no `remote_port` arg)
- Other tests unchanged (handle_response, cleanup_expired use request_id, not mapping)

## Verification

1. Run existing tests: `python -m pytest tests/` - all 37 tests should pass
2. Start local server+client, open GUI:
   - Open Tunnel modal
   - Add a tunnel with service port (e.g. 8999)
   - Verify auto-generated tunnel_id appears in the list
   - Access `/tunnel/{tunnel_id}/` in browser - should proxy to client's local service
   - Delete the tunnel - should remove from list

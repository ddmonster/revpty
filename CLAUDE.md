# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Build package
bash build.sh --version X.Y.Z

# Run all tests
python -m pytest tests/ -v

# Run single test file
python -m pytest tests/test_protocol.py -v

# Run single test
python -m pytest tests/test_protocol.py::ProtocolTests::test_encode_decode_input -v

# Check syntax
python3 -m py_compile revpty/cli/main.py revpty/client/agent.py
```

## Release Process

```bash
# 1. Build
bash build.sh --version X.Y.Z

# 2. Commit & push
git add -A && git commit -m "chore: bump version to X.Y.Z"
git push

# 3. Create GitHub release
gh release create vX.Y.Z dist/revpty-X.Y.Z-py3-none-any.whl dist/revpty-X.Y.Z.tar.gz \
  --title "vX.Y.Z" --notes "Release notes here"

# 4. Publish to PyPI
twine upload dist/revpty-X.Y.Z.tar.gz dist/revpty-X.Y.Z-py3-none-any.whl \
  --username __token__ --password $TWINE_PASSWORD
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Server (app.py)                           │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────────┐  │
│  │   Router    │  │ SessionMgr   │  │ TunnelMgr                  │  │
│  │ (routes)    │  │ (PTY mgmt)   │  │ (HTTP tunnel to client)    │  │
│  └─────────────┘  └──────────────┘  └────────────────────────────┘  │
│         │                │                         │                 │
│         ▼                ▼                         ▼                 │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              WebSocket Handler (/ws)                          │   │
│  └──────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │ WebSocket
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Client (agent.py)                          │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    ConnectionMux (mux.py)                    │    │
│  │  - Single WS for all sessions                                │    │
│  │  - Priority queue: I/O > file/tunnel                         │    │
│  │  - Offline buffering during disconnect                        │    │
│  │  - 10s heartbeat with dead connection detection               │    │
│  └─────────────────────────────────────────────────────────────┘    │
│         │                                                            │
│         ▼                                                            │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────────┐  │
│  │  PTYShell   │  │ FileManager  │  │ TunnelProxy                │  │
│  │ (local PTY) │  │ (file I/O)   │  │ (forward to local service) │  │
│  └─────────────┘  └──────────────┘  └────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## Key Components

### Protocol Layer (`revpty/protocol/`)

- **Frame** (`frame.py`): Dataclass with `session`, `role`, `type`, `data`, `rows`, `cols`
- **FrameType**: Enum - INPUT, OUTPUT, RESIZE, PING, PONG, STATUS, FILE, CONTROL, ATTACH, DETACH
- **Role**: CLIENT (PTY owner), BROWSER (terminal user), VIEWER (read-only)
- **Codec** (`codec.py`): JSON-based encode/decode with base64 for binary data

### Server (`revpty/server/`)

- **app.py**: aiohttp server with routes for `/ws`, `/gui`, `/api/*`, `/tunnel/*`
- **router.py**: WebSocket routing by session ID
- **session/manager.py**: PTY lifecycle, attach/detach, output cache for replay

### Client (`revpty/client/`)

- **agent.py**: Main client, handles control messages, spawns ShellWorker for sub-sessions
- **mux.py**: ConnectionMux - multiplexes sessions over single WS with priority queuing
- **pty_shell.py**: Subprocess PTY with fcntl/termios
- **file_manager.py**: Chunked file transfer with CRC32 verification
- **tunnel_proxy.py**: HTTP request forwarding to local services

### CLI (`revpty/cli/`)

- **main.py**: Entry points for `revpty-server`, `revpty-client`, `revpty-attach`
- **attach.py**: Interactive terminal attachment with TTY raw mode

## Important Patterns

### WebSocket Multiplexing

All sessions share one WebSocket via `ConnectionMux`. Each session registers a queue:
```python
queue = mux.register(session_id)
# Frames are routed to this queue for processing
```

### Priority Queuing (N5)

Frame types have priorities to ensure terminal I/O responsiveness:
- HIGH (0): INPUT, OUTPUT, RESIZE, PING, PONG, CONTROL
- LOW (1): FILE

### Network Resilience

- N1: Per-message deflate compression
- N2: Client-side output buffering during disconnect (256KB per session)
- N3: 10s heartbeat, reconnect after 2 missed pongs
- N4: Immediate first retry, then exponential backoff (max 10s)

### Adding New Frame Type

1. Add to `FrameType` enum in `frame.py`
2. Add validation rules in `Frame.validate()`
3. Handle in relevant dispatcher (server `router.py` or client `agent.py`)

## Authentication

- `--secret`: Custom `X-Revpty-Secret` header
- `--cf-client-id`/`--cf-client-secret`: Cloudflare Access Service Token headers
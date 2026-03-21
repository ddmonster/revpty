# revpty

Reverse PTY shell over WebSocket with web GUI, file transfer, and HTTP tunneling.

## Installation

```bash
pip install revpty
```

## Quick Start

**1. Start server:**
```bash
revpty-server --host 0.0.0.0 --port 8765
```

**2. Start client (on target machine):**
```bash
revpty-client --server ws://your-server:8765 --session my-session
```

**3. Attach to PTY (from your machine):**
```bash
revpty-attach --server ws://your-server:8765 --session my-session
```

**Or use the Web GUI:**
```
http://your-server:8765/gui?session=my-session
```

## CLI Reference

### revpty-server

```bash
revpty-server --host 0.0.0.0 --port 8765 [--secret YOUR_SECRET] [--cache-size 131072]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | 0.0.0.0 | Listen address |
| `--port` | 8765 | Listen port |
| `--secret` | - | Authentication secret |
| `--cache-size` | 131072 | Output cache size (bytes) |
| `--install` | - | Install as systemd service |
| `--user` | - | Install as user-level service |

### revpty-client

```bash
revpty-client --server ws://server:8765 --session NAME [options]
```

| Option | Description |
|--------|-------------|
| `--server` | Server URL (required) |
| `--session` | Session name (required) |
| `--secret` | Authentication secret |
| `--exec` | Shell to execute (default: /bin/bash) |
| `--proxy` | HTTP proxy URL |
| `--cf-client-id` | Cloudflare Access Client ID |
| `--cf-client-secret` | Cloudflare Access Client Secret |
| `--install` | Install as systemd service |
| `--user` | Install as user-level service |

### revpty-attach

```bash
revpty-attach --server ws://server:8765 --session NAME [options]
```

Options same as `revpty-client` (except `--exec`).

## Features

### Web GUI

Access the web-based terminal at:
```
http://your-server:8765/gui?session=my-session&secret=YOUR_SECRET
```

Features:
- Terminal emulator with resize support
- File browser (upload/download)
- Multiple shell sessions
- HTTP tunnel management
- Share links (read-only or read-write)

### Cloudflare Access Authentication

For servers protected by Cloudflare Zero Trust:

```bash
revpty-client --server wss://your-server:8765 --session my-session \
    --cf-client-id YOUR_CLIENT_ID.access \
    --cf-client-secret YOUR_CLIENT_SECRET
```

### File Transfer

**Via Web GUI:**
- Browse files in the file panel
- Upload: click upload button or drag & drop
- Download: click file name
- Large files use chunked transfer with CRC32 verification

**Chunked transfer features:**
- Automatic resume on reconnect
- Adaptive chunk size based on RTT
- Progress bar display

### HTTP Tunneling

Forward HTTP requests to services on the client machine:

**Via Web GUI:**
1. Click "Tunnel" button
2. Enter local service port (e.g., 8080)
3. Access via `/tunnel/{tunnel_id}/`

**Via API:**
```bash
# Create tunnel
curl -X POST http://server:8765/api/tunnels \
  -H "Content-Type: application/json" \
  -H "X-Revpty-Secret: YOUR_SECRET" \
  -d '{"session_id":"my-session","local_port":8080}'

# Response: {"tunnel_id":"a1b2c3d4","url":"/tunnel/a1b2c3d4/"}

# Access tunnel
curl http://server:8765/tunnel/a1b2c3d4/
```

### Multiple Shell Sessions

From the Web GUI, click "+" to create additional shell sessions. Each runs independently with its own PTY.

### Reconnect & Output Buffer

- **Client-side buffer**: Output during network disconnect is buffered and sent on reconnect
- **Server-side cache**: Recent output cached for replay when attaching
- **Auto-reconnect**: Exponential backoff with immediate first retry
- **Dead connection detection**: 10s heartbeat, reconnect after 2 missed pongs

### Systemd Service

Install as a system service:

```bash
# Server (as root)
revpty-server --host 0.0.0.0 --port 8765 --install

# Client (user-level)
revpty-client --server ws://server:8765 --session my-session --install --user
```

### HTTP Proxy Support

Client can connect via HTTP proxy:

```bash
revpty-client --server ws://server:8765 --session my-session --proxy http://proxy:8080
```

## Architecture

```
┌─────────────┐     WebSocket      ┌─────────────┐
│   Server    │◄──────────────────►│   Client    │
│  (revpty)   │                    │  (target)   │
└─────────────┘                    └─────────────┘
       │                                  │
       │                                  │
       ▼                                  ▼
  ┌─────────┐                        ┌─────────┐
  │Web GUI /│                        │   PTY   │
  │  Attach │                        │  Shell  │
  └─────────┘                        └─────────┘
```

## License

MIT
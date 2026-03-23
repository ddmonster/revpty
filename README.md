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
http://your-server:8765/revpty/gui?session=my-session
```

## CLI Reference

### revpty-server

```bash
revpty-server --host 0.0.0.0 --port 8765 [--secret YOUR_SECRET] [--cache-size 131072]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--config` | - | Load settings from TOML/JSON config file |
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
| `--config` | Load settings from TOML/JSON config file |
| `--server` | Server URL (required) |
| `--session` | Session name (required) |
| `--secret` | Authentication secret |
| `--exec` | Shell to execute (default: /bin/bash) |
| `--tunnel` | Register HTTP tunnel (format: port or host:port, can repeat) |
| `--proxy` | HTTP proxy URL |
| `--cf-client-id` | Cloudflare Access Client ID |
| `--cf-client-secret` | Cloudflare Access Client Secret |
| `--insecure` | Skip SSL certificate verification |
| `--install` | Install as systemd service |
| `--user` | Install as user-level service |

### revpty-attach

```bash
revpty-attach --server ws://server:8765 --session NAME [options]
```

Options same as `revpty-client` (except `--exec`, `--tunnel`).

## Configuration File

Both server and client support TOML or JSON configuration files:

**Server config (`/etc/revpty/server.toml`):**
```toml
host = "0.0.0.0"
port = 8765
secret = "your-secret-key"
cache_size = 131072
```

**Client config (`/etc/revpty/client.toml`):**
```toml
server = "https://net.example.com"
session = "my-session"
secret = "your-secret"
insecure = true
cf_client_id = "xxx.access"
cf_client_secret = "yyy"
tunnels = ["8080", "3000"]
```

**Usage:**
```bash
# Run with config
revpty-server --config /etc/revpty/server.toml
revpty-client --config /etc/revpty/client.toml

# Install as systemd service (uses config file in ExecStart)
revpty-server --config /etc/revpty/server.toml --install
revpty-client --config /etc/revpty/client.toml --install --user
```

## Features

### Web GUI

Access the web-based terminal at:
```
http://your-server:8765/revpty/gui?session=my-session&secret=YOUR_SECRET
```

Features:
- Terminal emulator with local echo for low-latency typing
- Waiting indicator when commands are running
- File browser (upload/download)
- Multiple shell sessions
- HTTP tunnel management
- Share links (read-only or read-write)

### Cloudflare Access Authentication

For servers protected by Cloudflare Zero Trust:

```bash
revpty-client --server wss://your-server.com --session my-session \
    --cf-client-id YOUR_CLIENT_ID.access \
    --cf-client-secret YOUR_CLIENT_SECRET \
    --insecure
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

Forward HTTP requests to services on the client machine.

**Auto-register with client:**
```bash
revpty-client --server ws://server:8765 --session my-session \
    --tunnel 8080 --tunnel 3000
```

Tunnels are automatically re-registered on reconnect.

**Via Web GUI:**
1. Click "Tunnel" button
2. Enter local service port (e.g., 8080)
3. Access via `/{tunnel_id}/`

**Via API:**
```bash
# Create tunnel
curl -X POST http://server:8765/revpty/api/tunnels?secret=YOUR_SECRET \
  -H "Content-Type: application/json" \
  -d '{"session_id":"my-session","local_port":8080}'

# Response: {"tunnel_id":"a1b2c3d4",...}

# Access tunnel (URL path or header)
curl http://server:8765/a1b2c3d4/
curl -H "X-Tunnel-Id: a1b2c3d4" http://server:8765/api/users
```

**Tunnel URL Resolution Priority:**
1. URL path: `/{tunnel_id}/path` (highest)
2. Header: `X-Tunnel-Id: {tunnel_id}`
3. Cookie: `tunnel_id`

### Multiple Shell Sessions

From the Web GUI, click "+" to create additional shell sessions. Each runs independently with its own PTY.

### Reconnect & Output Buffer

- **Client-side buffer**: Output during network disconnect is buffered and sent on reconnect
- **Server-side cache**: Recent output cached for replay when attaching
- **Auto-reconnect**: Exponential backoff with immediate first retry
- **Dead connection detection**: 10s heartbeat, reconnect after 2 missed pongs
- **Tunnel persistence**: Tunnels auto-re-register after reconnect

### Systemd Service

Install as a system service:

```bash
# Server (as root)
revpty-server --config /etc/revpty/server.toml --install

# Client (user-level)
revpty-client --config /etc/revpty/client.toml --install --user
```

### HTTP Proxy Support

Client can connect via HTTP proxy:

```bash
revpty-client --server ws://server:8765 --session my-session --proxy http://proxy:8080
```

## API Endpoints

All API endpoints are under `/revpty/` prefix:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/revpty/ws` | GET | WebSocket connection |
| `/revpty/gui` | GET | Web GUI |
| `/revpty/api/sessions` | GET | List sessions |
| `/revpty/api/tunnels` | GET/POST | List/create tunnels |
| `/revpty/api/tunnels/{id}` | DELETE | Delete tunnel |
| `/revpty/api/shares` | POST | Create share link |
| `/revpty/share/{id}` | GET | Resolve share link |
| `/{tunnel_id}/...` | * | Tunnel proxy |

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
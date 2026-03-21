# revpty

Reverse PTY shell over WebSocket.

## Installation

```bash
pip install revpty
```

## Usage

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

## Cloudflare Access Authentication

If your server is protected by Cloudflare Zero Trust with Service Token authentication:

```bash
revpty-client --server wss://your-server:8765 --session my-session \
    --cf-client-id YOUR_CLIENT_ID.access \
    --cf-client-secret YOUR_CLIENT_SECRET
```

Same for attach:
```bash
revpty-attach --server wss://your-server:8765 --session my-session \
    --cf-client-id YOUR_CLIENT_ID.access \
    --cf-client-secret YOUR_CLIENT_SECRET
```

The client will send `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers during WebSocket connection.

## Features

- WebSocket-based PTY relay
- Session-based routing
- Reconnect support
- HTTP proxy support for client
- Cloudflare Access Service Token authentication
- Minimal architecture

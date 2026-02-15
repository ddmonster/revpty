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

## Features

- WebSocket-based PTY relay
- Session-based routing
- Reconnect support
- HTTP proxy support for client
- Minimal architecture

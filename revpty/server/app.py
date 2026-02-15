import logging
import asyncio
import json
import time
import os
import hmac
from aiohttp import web
from aiohttp import WSMsgType
from revpty.protocol.codec import decode, encode, ProtocolError
from revpty.protocol.frame import Frame, FrameType, Role
from revpty.session import SessionManager, SessionConfig
from revpty.client.pty_shell import PTYShell
from revpty import __version__
_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(
    level=_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
SECRET_KEY = web.AppKey("revpty_secret", str)


def pty_factory(shell):
    """Factory function to create PTY instances"""
    return PTYShell(shell)


# Global Session Manager
session_manager = None


async def print_status():
    """Periodically print active sessions"""
    while True:
        await asyncio.sleep(30)
        if session_manager and session_manager.sessions:
            active = list(session_manager.sessions.keys())
            logger.info(f"[*] Active sessions: {', '.join(sorted(active))}")

GUI_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>revpty GUI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
  <style>
    html, body { height: 100%; margin: 0; background: #0b0e11; color: #e6e6e6; font-family: -apple-system, system-ui, sans-serif; }
    .bar { display: flex; gap: 8px; align-items: center; padding: 10px 12px; background: #121720; border-bottom: 1px solid #1f2833; }
    .bar input { background: #0b0e11; color: #e6e6e6; border: 1px solid #2a3441; padding: 6px 8px; border-radius: 6px; }
    .bar button { background: #2b7cff; color: white; border: 0; padding: 6px 10px; border-radius: 6px; cursor: pointer; }
    .bar button:disabled { background: #2a3441; cursor: default; }
    #status-container { margin-left: auto; display: flex; align-items: center; gap: 10px; }
    #status { color: #9bb3c7; font-size: 12px; }
    .led { width: 10px; height: 10px; border-radius: 50%; background: #333; transition: background 0.3s; display: inline-block; margin-right: 5px; }
    .led.on { background: #00ff00; box-shadow: 0 0 5px #00ff00; }
    .led.off { background: #ff0000; box-shadow: 0 0 5px #ff0000; }
    .led.busy { background: #ffaa00; box-shadow: 0 0 5px #ffaa00; }
    .label { font-size: 11px; color: #666; margin-right: 4px; }
    #workspace { height: calc(100% - 52px); display: flex; }
    #shell-sidebar { width: 220px; border-right: 1px solid #1f2833; background: #0f141b; display: flex; flex-direction: column; }
    .shell-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-bottom: 1px solid #1f2833; font-size: 12px; }
    .shell-header button { background: #2b7cff; color: white; border: 0; padding: 4px 8px; border-radius: 6px; cursor: pointer; }
    .shell-header button:disabled { background: #2a3441; cursor: default; }
    #shell-list { flex: 1; overflow-y: auto; }
    .shell-item { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; font-size: 12px; color: #e6e6e6; border-bottom: 1px solid #141b24; cursor: pointer; }
    .shell-item:hover { background: #141b24; }
    .shell-item.active { background: #1a212d; border-left: 3px solid #2b7cff; padding-left: 7px; }
    .shell-actions { display: flex; gap: 6px; }
    .shell-btn { background: none; border: 0; color: #9bb3c7; cursor: pointer; font-size: 12px; }
    #main-area { flex: 1; position: relative; }
    #terminal { height: 100%; width: 100%; }
    #dashboard { display: none; padding: 20px; max-width: 800px; margin: 0 auto; }
    #dashboard h1 { font-size: 20px; color: #fff; margin-bottom: 20px; }
    .session-card { background: #121720; border: 1px solid #1f2833; border-radius: 8px; padding: 15px; margin-bottom: 10px; cursor: pointer; transition: background 0.2s; display: flex; justify-content: space-between; align-items: center; }
    .session-card:hover { background: #1a212d; border-color: #2b7cff; }
    .session-info { display: flex; flex-direction: column; gap: 4px; }
    .session-id { font-weight: bold; color: #2b7cff; font-size: 16px; }
    .session-meta { font-size: 12px; color: #666; }
    .session-stats { display: flex; gap: 15px; align-items: center; }
    .stat-pill { background: #0b0e11; padding: 4px 8px; border-radius: 4px; font-size: 12px; color: #9bb3c7; border: 1px solid #2a3441; }
    .empty-state { text-align: center; color: #666; padding: 40px; }
    
    /* File Explorer */
    #file-explorer { position: absolute; top: 53px; right: 0; bottom: 0; width: 300px; background: #121720; border-left: 1px solid #1f2833; display: none; flex-direction: column; }
    .fe-header { padding: 10px; border-bottom: 1px solid #1f2833; display: flex; justify-content: space-between; align-items: center; font-size: 12px; font-weight: bold; }
    .fe-path { padding: 8px; background: #0b0e11; font-size: 11px; color: #9bb3c7; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .fe-list { flex: 1; overflow-y: auto; }
    .fe-item { padding: 4px 10px; display: flex; align-items: center; gap: 6px; font-size: 12px; color: #e6e6e6; cursor: default; }
    .fe-item:hover { background: #1f2833; }
    .fe-name { flex: 1; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .fe-actions { display: none; gap: 4px; }
    .fe-item:hover .fe-actions { display: flex; }
    .fe-btn { background: none; border: 0; padding: 2px; cursor: pointer; font-size: 12px; opacity: 0.7; }
    .fe-btn:hover { opacity: 1; }
    .fe-icon { width: 14px; text-align: center; color: #9bb3c7; }
    .fe-item.dir .fe-icon { color: #2b7cff; }
    
    /* Editor Modal */
    #editor-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); z-index: 100; align-items: center; justify-content: center; }
    .editor-container { width: 80%; height: 80%; background: #121720; border: 1px solid #1f2833; border-radius: 8px; display: flex; flex-direction: column; }
    .editor-header { padding: 10px; border-bottom: 1px solid #1f2833; display: flex; justify-content: space-between; align-items: center; }
    .editor-content { flex: 1; padding: 0; background: #0b0e11; border: 0; color: #e6e6e6; font-family: monospace; font-size: 13px; resize: none; outline: none; padding: 10px; }
    .btn-sm { padding: 4px 8px; font-size: 11px; }
  </style>
</head>
<body>
      <div class="bar">
    <button id="connect">Connect</button>
    <button id="disconnect" disabled>Disconnect</button>
    <button id="btn-files" disabled>Files</button>
    <div id="status-container">
        <div><span class="label">WS</span><span id="led-ws" class="led off"></span></div>
        <div><span class="label">CLIENT</span><span id="led-client" class="led off"></span></div>
        <div id="status">idle</div>
    </div>
  </div>
  <div id="workspace">
      <div id="shell-sidebar">
          <div class="shell-header">
              <span>Shells</span>
              <button id="btn-new-shell" disabled>New</button>
          </div>
          <div id="shell-list"></div>
      </div>
      <div id="main-area">
          <div id="terminal"></div>
      </div>
  </div>
  <div id="file-explorer">
      <div class="fe-header">
          <span>File Explorer</span>
          <div>
              <button class="btn-sm" onclick="triggerUpload()" title="Upload File">⬆️</button>
              <button class="btn-sm" onclick="hideFiles()">Close</button>
          </div>
      </div>
      <div class="fe-path" id="fe-current-path">/</div>
      <div class="fe-list" id="fe-list"></div>
      <input type="file" id="upload-input" style="display: none" />
  </div>
  <div id="editor-modal">
      <div class="editor-container">
          <div class="editor-header">
              <span id="editor-filename">filename.txt</span>
              <div>
                  <button class="btn-sm" onclick="saveFile()">Save</button>
                  <button class="btn-sm" onclick="closeEditor()">Close</button>
              </div>
          </div>
          <textarea class="editor-content" id="editor-text"></textarea>
      </div>
  </div>
  <div id="dashboard">
      <h1>Active Sessions</h1>
      <div id="session-list"></div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
  <script>
    const term = new Terminal({ cursorBlink: true, fontFamily: "monospace", fontSize: 14, scrollback: 50000, theme: { background: "#0b0e11" } })
    const fitAddon = new FitAddon.FitAddon()
    term.loadAddon(fitAddon)
    term.open(document.getElementById("terminal"))
    fitAddon.fit()

    const connectBtn = document.getElementById("connect")
    const disconnectBtn = document.getElementById("disconnect")
    const statusEl = document.getElementById("status")
    const ledWs = document.getElementById("led-ws")
    const ledClient = document.getElementById("led-client")
    const terminalEl = document.getElementById("terminal")
    const dashboardEl = document.getElementById("dashboard")
    const sessionListEl = document.getElementById("session-list")
    const shellListEl = document.getElementById("shell-list")
    const btnNewShell = document.getElementById("btn-new-shell")
    
    const qs = new URLSearchParams(location.search)
    const secretParam = qs.get("secret") || qs.get("seceret")
    let currentSession = qs.get("session") || ""

    let ws = null
    let receivedOutput = false
    let localEcho = true
    let promptTimer = null
    let shells = new Map()
    let shellBuffers = new Map()
    let activeShell = null

    function setStatus(text) { statusEl.textContent = text }
    function setLed(el, state) {
        el.className = "led " + state
    }

    const btnFiles = document.getElementById("btn-files")
    const feEl = document.getElementById("file-explorer")
    const fePathEl = document.getElementById("fe-current-path")
    const feListEl = document.getElementById("fe-list")
    const editorModal = document.getElementById("editor-modal")
    const editorText = document.getElementById("editor-text")
    const editorFilename = document.getElementById("editor-filename")
    const uploadInput = document.getElementById("upload-input")
    
    let currentPath = "."
    let currentFile = null
    
    // File Explorer Logic
    function toggleFiles() {
        if (feEl.style.display === "flex") hideFiles()
        else showFiles()
    }
    
    function showFiles() {
        feEl.style.display = "flex"
        refreshFiles()
    }
    
    function hideFiles() {
        feEl.style.display = "none"
    }
    
    function refreshFiles() {
        if (!ws || ws.readyState !== WebSocket.OPEN) return
        sendJson({ op: "list", path: currentPath, id: Date.now().toString() })
    }
    
    function renderFileList(entries, path) {
        currentPath = path
        fePathEl.textContent = path
        feListEl.innerHTML = ""
        
        // Parent dir
        if (path !== "/" && path !== ".") {
            const upDiv = document.createElement("div")
            upDiv.className = "fe-item dir"
            upDiv.innerHTML = `<span class="fe-icon">📁</span> <span class="fe-name">..</span>`
            upDiv.onclick = () => {
                const normalized = path.split("\\\\").join("/")
                const parts = normalized.split("/")
                parts.pop()
                const newPath = parts.join("/") || "/"
                sendJson({ op: "list", path: newPath, id: Date.now().toString() })
            }
            feListEl.appendChild(upDiv)
        }
        
        entries.forEach(e => {
            const div = document.createElement("div")
            div.className = "fe-item " + (e.is_dir ? "dir" : "file")
            const icon = e.is_dir ? "📁" : "📄"
            
            // Name part
            const nameSpan = document.createElement("span")
            nameSpan.className = "fe-name"
            nameSpan.textContent = e.name
            
            // Actions part
            const actionsDiv = document.createElement("div")
            actionsDiv.className = "fe-actions"
            
            if (!e.is_dir) {
                const dlBtn = document.createElement("button")
                dlBtn.className = "fe-btn"
                dlBtn.textContent = "⬇️"
                dlBtn.title = "Download"
                dlBtn.onclick = (evt) => {
                    evt.stopPropagation()
                    const sep = path.endsWith("/") ? "" : "/"
                    downloadFile(path + sep + e.name)
                }
                actionsDiv.appendChild(dlBtn)
            }
            
            div.innerHTML = `<span class="fe-icon">${icon}</span>`
            div.appendChild(nameSpan)
            div.appendChild(actionsDiv)
            
            // Click on name/row to open
            nameSpan.onclick = () => {
                const sep = path.endsWith("/") ? "" : "/"
                const fullPath = path + sep + e.name
                if (e.is_dir) {
                    sendJson({ op: "list", path: fullPath, id: Date.now().toString() })
                } else {
                    openFile(fullPath)
                }
            }
            
            feListEl.appendChild(div)
        })
    }
    
    function downloadFile(path) {
        sendJson({ op: "read", path: path, id: "dl_" + Date.now() + "_" + encodeURIComponent(path.split("/").pop()) })
    }

    function triggerUpload() {
        uploadInput.value = ""
        uploadInput.click()
    }
    
    uploadInput.addEventListener("change", () => {
        if (uploadInput.files.length === 0) return
        const file = uploadInput.files[0]
        const reader = new FileReader()
        
        reader.onload = () => {
            // reader.result is DataURL: data:content/type;base64,.....
            const b64 = reader.result.split(",")[1]
            const sep = currentPath.endsWith("/") ? "" : "/"
            const fullPath = currentPath + sep + file.name
            
            sendJson({ 
                op: "write", 
                path: fullPath, 
                content: b64, 
                id: "up_" + Date.now() 
            })
            setStatus("uploading " + file.name)
        }
        
        reader.readAsDataURL(file)
    })

    function openFile(path) {
        currentFile = path
        editorFilename.textContent = path.split("/").pop()
        editorText.value = "Loading..."
        editorModal.style.display = "flex"
        sendJson({ op: "read", path: path, id: Date.now().toString() })
    }
    
    function closeEditor() {
        editorModal.style.display = "none"
        currentFile = null
    }
    
    function saveFile() {
        if (!currentFile) return
        const content = editorText.value
        const b64 = encodeUtf8ToBase64(content)
        sendJson({ op: "write", path: currentFile, content: b64, id: Date.now().toString() })
    }
    
    function sendJson(obj) {
        if (!ws) return
        const session = currentSession.trim()
        // Read-only check for file operations
        if (document.body.classList.contains('readonly-mode') && obj.op !== 'list' && obj.op !== 'read') return
        
        ws.send(encodeFrame({ 
            session, 
            role: getRole(), 
            type: "file", 
            data: JSON.stringify(obj)
        }))
    }

    btnFiles.addEventListener("click", toggleFiles)

    function showDashboard() {
        terminalEl.style.display = "none"
        dashboardEl.style.display = "block"
        loadSessions()
    }

    function showTerminal() {
        terminalEl.style.display = "block"
        dashboardEl.style.display = "none"
        fitAddon.fit()
    }

    async function loadSessions() {
        try {
            const url = "/api/sessions" + (secretParam ? "?secret=" + encodeURIComponent(secretParam) : "")
            const res = await fetch(url)
            if (!res.ok) throw new Error("Failed to load sessions")
            const sessions = await res.json()
            
            sessionListEl.innerHTML = ""
            if (sessions.length === 0) {
                sessionListEl.innerHTML = '<div class="empty-state">No active sessions found. Start a client to see it here.</div>'
                shells.clear()
                shellBuffers.clear()
                renderShellList()
                return
            }
            
            shells.clear()
            shellBuffers.clear()
            sessions.forEach(s => {
                shells.set(s.id, { id: s.id })
                shellBuffers.set(s.id, "")
                const card = document.createElement("div")
                card.className = "session-card"
                const activeTime = new Date(s.active_at * 1000).toLocaleTimeString()
                card.innerHTML = `
                    <div class="session-info">
                        <div class="session-id">${s.id}</div>
                        <div class="session-meta">Shell: ${s.shell} • Last active: ${activeTime}</div>
                    </div>
                    <div class="session-stats">
                        <div class="stat-pill">Clients: ${s.clients}</div>
                        <div class="stat-pill">Browsers: ${s.browsers}</div>
                        <div class="stat-pill" style="color: ${s.state === 'running' ? '#00ff00' : '#ffaa00'}">${s.state}</div>
                    </div>
                `
                card.onclick = () => {
                    currentSession = s.id
                    connect()
                }
                sessionListEl.appendChild(card)
            })
            renderShellList()
        } catch (e) {
            sessionListEl.innerHTML = `<div class="empty-state" style="color: #ff5555">Error loading sessions: ${e.message}</div>`
        }
    }

    function wsUrl() {
      const scheme = location.protocol === "https:" ? "wss://" : "ws://"
      return scheme + location.host + "/ws"
    }

    function withSecret(url) {
      if (!secretParam) return url
      if (url.includes("secret=")) return url
      return url + (url.includes("?") ? "&" : "?") + "secret=" + encodeURIComponent(secretParam)
    }

    function encodeFrame(frame) {
      const obj = { v: 1, session: frame.session, role: frame.role, type: frame.type, ts: Date.now() / 1000 }
      if (frame.data != null) {
        const utf8 = new TextEncoder().encode(frame.data)
        let binary = ""
        for (let i = 0; i < utf8.length; i++) binary += String.fromCharCode(utf8[i])
        obj.data = btoa(binary)
      }
      if (frame.rows != null) obj.rows = frame.rows
      if (frame.cols != null) obj.cols = frame.cols
      return JSON.stringify(obj)
    }

    function decodeData(base64) {
      const binary = atob(base64)
      const bytes = new Uint8Array(binary.length)
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
      return new TextDecoder().decode(bytes)
    }

    function encodeUtf8ToBase64(text) {
      const bytes = new TextEncoder().encode(text)
      let binary = ""
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
      return btoa(binary)
    }

    function decodeBase64ToUtf8(b64) {
      const binary = atob(b64)
      const bytes = new Uint8Array(binary.length)
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
      return new TextDecoder().decode(bytes)
    }

    function getRole() {
        return document.body.classList.contains('readonly-mode') ? "viewer" : "browser"
    }

    function sendResize() {
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(encodeFrame({ session: currentSession, role: getRole(), type: "resize", rows: term.rows, cols: term.cols }))
    }

    function connect() {
      const session = currentSession.trim()
      if (!session) return
      const target = wsUrl()
      if (ws) {
        try { ws.close() } catch {}
        ws = null
      }
      
      showTerminal()
      term.reset()
      const cached = shellBuffers.get(session)
      if (cached) {
        term.write(cached)
        receivedOutput = true
      }

      const isReadOnly = document.body.classList.contains('readonly-mode')
      if (isReadOnly) {
          term.options.disableStdin = true
      } else {
          term.options.disableStdin = false
      }

      const wsTarget = withSecret(target)
      ws = new WebSocket(wsTarget)
      setStatus("connecting")
      setLed(ledWs, "busy")
      setLed(ledClient, "off")
      connectBtn.disabled = true
      disconnectBtn.disabled = false
      btnFiles.disabled = true
      ws.onopen = () => {
        ws.send(encodeFrame({ session, role: getRole(), type: "attach" }))
        sendResize()
        receivedOutput = false
        localEcho = true
        if (promptTimer) clearTimeout(promptTimer)
        promptTimer = setTimeout(() => {
          if (!receivedOutput && ws && ws.readyState === WebSocket.OPEN && !isReadOnly) {
            ws.send(encodeFrame({ session, role: "browser", type: "input", data: "\\n" }))
          }
        }, 600)
        setStatus("attached")
        setLed(ledWs, "on")
        if (!isReadOnly) btnFiles.disabled = false
        if (!isReadOnly) btnNewShell.disabled = false
        addShell(session)
        setActiveShell(session)
        term.focus()
      }
      ws.onmessage = (evt) => {
        const frame = JSON.parse(evt.data)
        if (frame.type === "output" && frame.data) {
          receivedOutput = true
          localEcho = false
          const chunk = decodeData(frame.data)
          term.write(chunk)
          if (session) {
            const prev = shellBuffers.get(session) || ""
            shellBuffers.set(session, prev + chunk)
          }
        } else if (frame.type === "ping") {
          ws.send(encodeFrame({ session, role: getRole(), type: "pong" }))
        } else if (frame.type === "file" && frame.data) {
          try {
            const data = JSON.parse(decodeData(frame.data))
            if (data.op === "list_ack") {
              renderFileList(data.entries, data.path)
            } else if (data.op === "read_ack") {
              // Check if it's a download request
              if (data.id && data.id.startsWith("dl_")) {
                  try {
                      // Extract filename from ID: dl_TIMESTAMP_FILENAME
                      const parts = data.id.split("_")
                      const filename = decodeURIComponent(parts.slice(2).join("_"))
                      
                      // Trigger browser download
                      const link = document.createElement("a")
                      link.href = "data:application/octet-stream;base64," + data.content
                      link.download = filename
                      document.body.appendChild(link)
                      link.click()
                      document.body.removeChild(link)
                      setStatus("downloaded " + filename)
                  } catch(e) {
                      alert("Download failed: " + e.message)
                  }
                  return
              }
              
              // Normal editor open
              try {
                editorText.value = decodeBase64ToUtf8(data.content)
              } catch(e) {
                editorText.value = "[Error decoding content]"
              }
            } else if (data.op === "write_ack") {
              if (data.id && data.id.startsWith("up_")) {
                  setStatus("upload complete")
                  refreshFiles()
              } else {
                  alert("File saved!")
              }
            } else if (data.op === "error") {
              alert("Error: " + data.error)
            }
          } catch(e) {}
        } else if (frame.type === "control" && frame.data) {
          try {
            const data = JSON.parse(decodeData(frame.data))
            if (data.op === "new_shell_ack") {
              if (data.ok) {
                addShell(data.session)
                setStatus("shell created")
              } else if (data.error) {
                setStatus("shell create failed")
              }
            } else if (data.op === "close_shell_ack") {
              removeShell(data.session)
              setStatus("shell closed")
            }
          } catch {}
        } else if (frame.type === "status" && frame.data) {
          try { 
              const status = JSON.parse(decodeData(frame.data))
              setStatus("peers " + status.peers)
              // If peers > 0 (excluding self), assume client is connected. 
              // Wait, 'peers' is peers of THIS role (browser). 
              // The server logic: peers = session.get_peer(frame.role)
              // In Session.get_peer(role): if role=browser, returns CLIENTS. 
              // So peers count IS the client count!
              if (status.peers > 0) {
                  setLed(ledClient, "on")
              } else {
                  setLed(ledClient, "off")
              }
          } catch {}
        }
      }
      ws.onclose = () => {
        setStatus("disconnected")
        setLed(ledWs, "off")
        setLed(ledClient, "off")
        connectBtn.disabled = false
        disconnectBtn.disabled = true
        btnNewShell.disabled = true
      }
      ws.onerror = () => {
          setStatus("error")
          setLed(ledWs, "off")
      }
    }

    function disconnect() {
      if (!ws) return
      const session = currentSession.trim()
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(encodeFrame({ session, role: getRole(), type: "detach" }))
      }
      ws.close()
      ws = null
      
      showDashboard()
    }

    connectBtn.addEventListener("click", connect)
    disconnectBtn.addEventListener("click", disconnect)
    window.addEventListener("resize", () => { fitAddon.fit(); sendResize() })
    term.onData(data => {
      const session = currentSession.trim()
      if (!ws || ws.readyState !== WebSocket.OPEN || !session) return
      // Read-only check
      if (document.body.classList.contains('readonly-mode')) return
      
      if (localEcho) {
        term.write(data)
        if (session) {
          const prev = shellBuffers.get(session) || ""
          shellBuffers.set(session, prev + data)
        }
      }
      ws.send(encodeFrame({ session, role: getRole(), type: "input", data }))
    })

    if (currentSession) {
        loadSessions()
        connect()
    } else {
        showDashboard()
    }
    
    // Auto-connect if query params present
    const urlParams = new URLSearchParams(window.location.search);
    const modeParam = urlParams.get('mode');
    if (modeParam === 'ro' || modeParam === 'readonly') {
        document.body.classList.add('readonly-mode');
        // We will handle role in connect()
    }

    function sendControl(session, payload) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(encodeFrame({
        session,
        role: getRole(),
        type: "control",
        data: JSON.stringify(payload)
      }))
    }

    function generateSessionId(base) {
      const suffix = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2))
      return base + "-" + suffix.slice(0, 8)
    }

    function addShell(id) {
      if (!id || shells.has(id)) return
      shells.set(id, { id })
      if (!shellBuffers.has(id)) shellBuffers.set(id, "")
      renderShellList()
    }

    function removeShell(id) {
      shells.delete(id)
      shellBuffers.delete(id)
      if (activeShell === id) activeShell = null
      renderShellList()
    }

    function setActiveShell(id) {
      activeShell = id
      renderShellList()
    }

    function renderShellList() {
      shellListEl.innerHTML = ""
      shells.forEach((shell) => {
        const row = document.createElement("div")
        row.className = "shell-item" + (shell.id === activeShell ? " active" : "")
        const name = document.createElement("span")
        name.textContent = shell.id
        const actions = document.createElement("div")
        actions.className = "shell-actions"
        const closeBtn = document.createElement("button")
        closeBtn.className = "shell-btn"
        closeBtn.textContent = "✖"
        closeBtn.onclick = (evt) => {
          evt.stopPropagation()
          sendControl(shell.id, { op: "close_shell" })
        }
        actions.appendChild(closeBtn)
        row.appendChild(name)
        row.appendChild(actions)
        row.onclick = () => {
          currentSession = shell.id
          connect()
        }
        shellListEl.appendChild(row)
      })
    }

    btnNewShell.addEventListener("click", () => {
      const base = currentSession.trim()
      if (!base) return
      const newSession = generateSessionId(base)
      sendControl(base, { op: "new_shell", session: newSession })
      setStatus("creating shell")
    })
  </script>
</body>
</html>
"""

async def gui_handler(request):
    return web.Response(text=GUI_HTML, content_type="text/html")


async def sessions_api_handler(request):
    """Return active sessions list"""
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret") or request.query.get("seceret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.json_response({"error": "unauthorized"}, status=401)
            
    sessions_data = []
    if session_manager:
        for sid, session in session_manager.sessions.items():
            sessions_data.append({
                "id": sid,
                "clients": len(session.clients),
                "browsers": len(session.browsers),
                "state": session.state.value,
                "active_at": int(session.last_active),
                "created_at": int(session.created_at),
                "shell": session.config.shell
            })
    
    # Sort by activity (newest first)
    sessions_data.sort(key=lambda x: x["active_at"], reverse=True)
    return web.json_response(sessions_data)


async def broadcast_status(session):
    """Broadcast session status to all peers"""
    # Notify browsers (peers = count of clients)
    browser_peers = len(session.clients)
    browser_payload = json.dumps({
        "session": session.id,
        "role": "browser",
        "peers": browser_peers,
        "state": session.state.value,
    }).encode("utf-8")
    
    browser_frame = encode(Frame(
        session=session.id,
        role="server",
        type=FrameType.STATUS.value,
        data=browser_payload,
    ))
    
    for browser in session.browsers:
        if not browser.closed:
            await browser.send_str(browser_frame)
            
    # Notify clients (peers = count of browsers)
    client_peers = len(session.browsers)
    client_payload = json.dumps({
        "session": session.id,
        "role": "client",
        "peers": client_peers,
        "state": session.state.value,
    }).encode("utf-8")
    
    client_frame = encode(Frame(
        session=session.id,
        role="server",
        type=FrameType.STATUS.value,
        data=client_payload,
    ))
    
    for client in session.clients:
        if not client.closed:
            await client.send_str(client_frame)


async def websocket_handler(request):
    """Handle WebSocket connections with Session-based routing"""
    ws = web.WebSocketResponse(heartbeat=30)
    can_prepare = ws.can_prepare(request)
    if not can_prepare.ok:
        return web.Response(text="revpty server", content_type="text/plain")
    required_secret = request.app.get(SECRET_KEY)
    if required_secret:
        provided = request.headers.get("X-Revpty-Secret") or request.query.get("secret") or request.query.get("seceret")
        if not provided or not hmac.compare_digest(provided, required_secret):
            return web.Response(status=401, text="unauthorized")
    await ws.prepare(request)
    remote_addr = request.remote or "unknown"
    
    logger.info(f"[+] Connection from {remote_addr}")
    
    current_session = None
    current_role = None
    
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    # Decode and validate frame
                    frame = decode(msg.data)
                    current_session = frame.session
                    current_role = frame.role
                    
                    # Handle attach/detach
                    if frame.type == FrameType.ATTACH.value:
                        session = await session_manager.attach(
                            frame.session, frame.role, ws
                        )
                        logger.info(f"[+] {frame.role.upper()} attached to '{frame.session}'")
                        await broadcast_status(session)
                        continue
                    
                    # Handle detach
                    if frame.type == FrameType.DETACH.value:
                        session_manager.detach(frame.session, frame.role, ws)
                        logger.info(f"[-] {frame.role.upper()} detached from '{frame.session}'")
                        session = session_manager.get(frame.session)
                        if session:
                            await broadcast_status(session)
                        continue
                    
                    # Route data frames through session
                    session = session_manager.get(frame.session)
                    if not session:
                        logger.warning(f"[!] Session '{frame.session}' not found")
                        continue
                    
                    # Update session activity
                    session.last_active = time.time()
                    
                    # Security: Enforce read-only for Viewer role
                    if frame.role == Role.VIEWER.value:
                        if frame.type in (FrameType.INPUT.value, FrameType.RESIZE.value):
                            # Viewers cannot send input or resize
                            continue
                        if frame.type == FrameType.FILE.value:
                            # Viewers can only list/read, not write
                            try:
                                file_op = json.loads(frame.data)
                                if file_op.get("op") not in ("list", "read"):
                                    continue
                            except:
                                continue

                    # Get peers and broadcast
                    peers = session.get_peer(frame.role)
                    if peers:
                        logger.debug(f"[>] {frame.session}: {frame.role} -> {frame.type} ({len(peers)} peers)")
                        for peer in list(peers):  # Copy to avoid modification during iteration
                            if not peer.closed:
                                await peer.send_str(msg.data)
                    elif frame.type not in (FrameType.PING.value, FrameType.PONG.value):
                        logger.debug(f"[!] No peers for {frame.session} {frame.role} -> {frame.type}")
                    
                except ProtocolError as e:
                    logger.error(f"[x] Protocol error: {e}")
                    continue
                    
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"[x] WebSocket error: {ws.exception()}")
                break
                
    except asyncio.CancelledError:
        logger.info(f"[*] Connection cancelled for {remote_addr}")
    except Exception as e:
        logger.error(f"[x] Handler error: {e}")
    finally:
        # Detach from session
        if current_session:
            session_manager.detach(current_session, current_role, ws)
        
        logger.info(f"[-] Connection closed: {remote_addr}")
    
    return ws


async def on_startup(app):
    """Start background tasks"""
    global session_manager
    
    # Initialize Session Manager
    config = SessionConfig(
        shell="/bin/bash",
        idle_timeout=3600,
        enable_log=True
    )
    session_manager = SessionManager(pty_factory, config)
    await session_manager.start_cleanup_task(interval=60)
    
    # Start status printer
    app['status_task'] = asyncio.create_task(print_status())


async def on_cleanup(app):
    """Cleanup background tasks"""
    global session_manager
    
    if 'status_task' in app:
        app['status_task'].cancel()
        try:
            await app['status_task']
        except asyncio.CancelledError:
            pass
    
    if session_manager:
        await session_manager.shutdown()


def run(host="0.0.0.0", port=8765, secret=None):
    version = __version__
    logger.info(f"[*] revpty server v{version} - Session-based Architecture")
    logger.info(f"[*] Listening on {host}:{port}")
    logger.info(f"[*] Protocol v1 with validation")
    logger.info(f"[*] Session lifecycle > Connection lifecycle")
    logger.info(f"[*] Ready for connections")
    
    app = web.Application()
    if secret:
        app[SECRET_KEY] = secret
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get('/', websocket_handler)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/gui', gui_handler)
    app.router.add_get('/api/sessions', sessions_api_handler)
    
    web.run_app(app, host=host, port=port, print=lambda s: None)

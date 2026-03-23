// Main application logic

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
// Clean ws= parameter from URL if present
if (qs.has("ws")) {
  qs.delete("ws")
  history.replaceState(null, '', location.pathname + '?' + qs.toString())
}
const secretParam = qs.get("secret")
let currentSession = qs.get("session") || ""

let ws = null
let receivedOutput = false
let suppressInputUntil = 0
let baseSession = currentSession || ""
let attachedSession = ""
let localEcho = true
let promptTimer = null
let shells = new Map()
let shellBuffers = new Map()
let activeShell = null
let waitingForOutput = false
let pendingInput = ""  // For local echo matching

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
    if ((!ws || ws.readyState !== WebSocket.OPEN) && (!wsFile || wsFile.readyState !== WebSocket.OPEN)) return
    sendFileJson({ op: "list", path: currentPath, id: Date.now().toString() })
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
            const normalized = path.split("\\").join("/")
            const parts = normalized.split("/")
            parts.pop()
            const newPath = parts.join("/") || "/"
            sendFileJson({ op: "list", path: newPath, id: Date.now().toString() })
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
                sendFileJson({ op: "list", path: fullPath, id: Date.now().toString() })
            } else {
                openFile(fullPath)
            }
        }
        
        feListEl.appendChild(div)
    })
}

function downloadFile(path) {
    // Use chunked downloader for files
    chunkedDownload(path)
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
        const sep = currentPath.endsWith("/") ? "" : "/"
        const fullPath = currentPath + sep + file.name
        // Use chunked upload for all files
        chunkedUpload(file, fullPath)
    }
    
    reader.readAsArrayBuffer(file)
})

function openFile(path) {
    currentFile = path
    editorFilename.textContent = path.split("/").pop()
    editorText.value = "Loading..."
    editorModal.style.display = "flex"
    sendFileJson({ op: "read", path: path, id: Date.now().toString() })
}

function closeEditor() {
    editorModal.style.display = "none"
    currentFile = null
}

function saveFile() {
    if (!currentFile) return
    const content = editorText.value
    const b64 = encodeUtf8ToBase64(content)
    sendFileJson({ op: "write", path: currentFile, content: b64, id: Date.now().toString() })
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

// --- File WebSocket (Phase 6: dual WS) ---
let wsFile = null
let wsFileSession = null
function connectFileWs() {
  const session = currentSession.trim()
  if (wsFile && wsFile.readyState === WebSocket.OPEN) {
    // Re-attach to current session if changed
    if (wsFileSession !== session && session) {
      wsFile.send(encodeFrame({ session, role: getRole(), type: "attach" }))
      wsFileSession = session
    }
    return
  }
  const target = location.protocol === "https:" ? "wss://" : "ws://"
  const url = target + location.host + "/revpty/ws/file" + (secretParam ? "?secret=" + encodeURIComponent(secretParam) : "")
  wsFile = new WebSocket(url)
  wsFile.onopen = () => {
    const session = currentSession.trim()
    if (session) {
      wsFile.send(encodeFrame({ session, role: getRole(), type: "attach" }))
      wsFileSession = session
    }
  }
  wsFile.onmessage = (evt) => {
    const frame = JSON.parse(evt.data)
    if (frame.type === "file" && frame.data) {
      handleFileResponse(frame)
    }
  }
  wsFile.onclose = () => { wsFile = null; wsFileSession = null }
  wsFile.onerror = () => { wsFile = null; wsFileSession = null }
}

function sendFileJson(obj) {
  const session = currentSession.trim()
  if (document.body.classList.contains('readonly-mode') && obj.op !== 'list' && obj.op !== 'read') return
  const frame = encodeFrame({ session, role: getRole(), type: "file", data: JSON.stringify(obj) })
  // Prefer file WS if available, fallback to main WS
  if (wsFile && wsFile.readyState === WebSocket.OPEN) {
    wsFile.send(frame)
  } else if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(frame)
  }
}

// --- Chunked Transfer (Phase 5) ---
const transferBar = document.getElementById("transfer-bar")
const transferFill = document.getElementById("transfer-fill")
const transferInfo = document.getElementById("transfer-info")
let activeTransfers = new Map()

function showTransferProgress(pct, text) {
  transferBar.style.display = "block"
  transferFill.style.width = pct + "%"
  transferInfo.style.display = "block"
  transferInfo.textContent = text
}
function hideTransferProgress() {
  transferBar.style.display = "none"
  transferInfo.style.display = "none"
}

function chunkedDownload(path) {
  const transferId = Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  activeTransfers.set(transferId, { path, chunks: [], totalChunks: 0, totalSize: 0 })
  sendFileJson({ op: "file_init", transfer_id: transferId, path, direction: "download", chunk_size: 65536 })
  showTransferProgress(0, "Initializing download...")
}

function chunkedUpload(file, destPath) {
  const transferId = Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  const chunkSize = 65536
  const reader = new FileReader()
  reader.onload = () => {
    const data = new Uint8Array(reader.result)
    const totalChunks = Math.ceil(data.length / chunkSize)
    activeTransfers.set(transferId, { file, data, chunkSize, totalChunks, sentSeq: 0, ackedSeqs: new Set(), path: destPath })
    sendFileJson({ op: "file_init", transfer_id: transferId, path: destPath, direction: "upload", chunk_size: chunkSize })
    showTransferProgress(0, "Initializing upload...")
  }
  reader.readAsArrayBuffer(file)
}

function sendUploadChunks(transferId, windowSize) {
  const xfer = activeTransfers.get(transferId)
  if (!xfer || !xfer.data) return
  let sent = 0
  const inFlight = xfer.sentSeq - xfer.ackedSeqs.size
  const canSend = (windowSize || 4) - inFlight
  while (sent < canSend && xfer.sentSeq < xfer.totalChunks) {
    const seq = xfer.sentSeq
    const offset = seq * xfer.chunkSize
    const chunk = xfer.data.slice(offset, offset + xfer.chunkSize)
    let binary = ""
    for (let i = 0; i < chunk.length; i++) binary += String.fromCharCode(chunk[i])
    const b64 = btoa(binary)
    // CRC32 - simple implementation
    const crc = crc32(chunk)
    sendFileJson({ op: "file_chunk", transfer_id: transferId, seq, data: b64, crc32: crc })
    xfer.sentSeq++
    sent++
  }
}

function handleFileResponse(frame) {
  try {
    const data = JSON.parse(decodeData(frame.data))
    if (data.op === "list_ack") {
      renderFileList(data.entries, data.path)
    } else if (data.op === "read_ack") {
      if (data.id && data.id.startsWith("dl_")) {
        try {
          const parts = data.id.split("_")
          const filename = decodeURIComponent(parts.slice(2).join("_"))
          const link = document.createElement("a")
          link.href = "data:application/octet-stream;base64," + data.content
          link.download = filename
          document.body.appendChild(link)
          link.click()
          document.body.removeChild(link)
          setStatus("downloaded " + filename)
        } catch(e) { alert("Download failed: " + e.message) }
        return
      }
      try { editorText.value = decodeBase64ToUtf8(data.content) }
      catch(e) { editorText.value = "[Error decoding content]" }
    } else if (data.op === "write_ack") {
      if (data.id && data.id.startsWith("up_")) {
        setStatus("upload complete")
        refreshFiles()
      } else { alert("File saved!") }
    } else if (data.op === "error") {
      alert("Error: " + data.error)
      hideTransferProgress()
    // Chunked transfer responses
    } else if (data.op === "file_init_ack") {
      const xfer = activeTransfers.get(data.transfer_id)
      if (!xfer) return
      // Only overwrite if server provided actual values (downloads).
      // For uploads, server returns 0 - keep browser's local computation.
      if (data.total_chunks > 0) xfer.totalChunks = data.total_chunks
      if (data.total_size > 0) xfer.totalSize = data.total_size
      if (xfer.data) {
        // Upload: start sending chunks
        sendUploadChunks(data.transfer_id, 4)
        showTransferProgress(0, "Uploading... 0/" + xfer.totalChunks)
      } else {
        // Download: send first ack to trigger chunks
        xfer.chunks = new Array(data.total_chunks)
        sendFileJson({ op: "file_chunk_ack", transfer_id: data.transfer_id, seq: -1 })
        showTransferProgress(0, "Downloading... 0/" + data.total_chunks)
      }
    } else if (data.op === "file_chunk") {
      // Received download chunk
      const xfer = activeTransfers.get(data.transfer_id)
      if (!xfer) return
      const binary = atob(data.data)
      const bytes = new Uint8Array(binary.length)
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
      // Verify CRC32
      const actualCrc = crc32(bytes)
      if (data.crc32 && actualCrc !== data.crc32) {
        sendFileJson({ op: "file_chunk_nack", transfer_id: data.transfer_id, seq: data.seq, reason: "crc_mismatch" })
        return
      }
      xfer.chunks[data.seq] = bytes
      const done = xfer.chunks.filter(c => c).length
      const pct = Math.round(done / xfer.totalChunks * 100)
      showTransferProgress(pct, "Downloading... " + done + "/" + xfer.totalChunks)
      sendFileJson({ op: "file_chunk_ack", transfer_id: data.transfer_id, seq: data.seq })
    } else if (data.op === "file_chunks_batch") {
      // Multiple chunks in one message
      if (data.chunks) data.chunks.forEach(c => handleFileResponse({ data: btoa(JSON.stringify(c)), type: "file" }))
    } else if (data.op === "file_chunk_ack") {
      // Upload chunk acknowledged
      const xfer = activeTransfers.get(data.transfer_id)
      if (!xfer || !xfer.data) return
      xfer.ackedSeqs.add(data.seq)
      const pct = Math.round(xfer.ackedSeqs.size / xfer.totalChunks * 100)
      showTransferProgress(pct, "Uploading... " + xfer.ackedSeqs.size + "/" + xfer.totalChunks)
      sendUploadChunks(data.transfer_id, 4)
      if (xfer.ackedSeqs.size >= xfer.totalChunks) {
        sendFileJson({ op: "file_complete", transfer_id: data.transfer_id, checksum: "" })
      }
    } else if (data.op === "file_chunk_nack") {
      // Retransmit needed - re-read and send
      const xfer = activeTransfers.get(data.transfer_id)
      if (!xfer || !xfer.data) return
      const seq = data.seq
      const offset = seq * xfer.chunkSize
      const chunk = xfer.data.slice(offset, offset + xfer.chunkSize)
      let bin = ""
      for (let i = 0; i < chunk.length; i++) bin += String.fromCharCode(chunk[i])
      sendFileJson({ op: "file_chunk", transfer_id: data.transfer_id, seq, data: btoa(bin), crc32: crc32(chunk) })
    } else if (data.op === "file_complete") {
      // Download complete - assemble and trigger browser download
      const xfer = activeTransfers.get(data.transfer_id)
      if (!xfer) return
      const totalLen = xfer.chunks.reduce((s, c) => s + (c ? c.length : 0), 0)
      const combined = new Uint8Array(totalLen)
      let off = 0
      xfer.chunks.forEach(c => { if (c) { combined.set(c, off); off += c.length } })
      const blob = new Blob([combined])
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = xfer.path.split("/").pop()
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(url)
      sendFileJson({ op: "file_complete_ack", transfer_id: data.transfer_id })
      activeTransfers.delete(data.transfer_id)
      hideTransferProgress()
      setStatus("download complete")
    } else if (data.op === "file_complete_ack") {
      activeTransfers.delete(data.transfer_id)
      hideTransferProgress()
      setStatus("upload complete")
      refreshFiles()
    } else if (data.op === "file_abort" || data.op === "file_abort_ack") {
      activeTransfers.delete(data.transfer_id)
      hideTransferProgress()
      setStatus("transfer aborted")
    }
  } catch(e) {}
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
        const url = "/revpty/api/sessions" + (secretParam ? "?secret=" + encodeURIComponent(secretParam) : "")
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
  return scheme + location.host + "/revpty/ws"
}

function withSecret(url) {
  if (!secretParam) return url
  if (url.includes("secret=")) return url
  return url + (url.includes("?") ? "&" : "?") + "secret=" + encodeURIComponent(secretParam)
}

function getRole() {
    return document.body.classList.contains('readonly-mode') ? "viewer" : "browser"
}

function sendResize() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return
  const session = attachedSession || currentSession
  if (!session) return
  ws.send(encodeFrame({ session, role: getRole(), type: "resize", rows: term.rows, cols: term.cols }))
}

function setupSessionView(session) {
  showTerminal()
  term.reset()
  fitAddon.fit()
  // Don't replay shellBuffers here - server will send output_buffer replay on ATTACH
  receivedOutput = false
  localEcho = true  // Enable local echo for better responsiveness
  pendingInput = ""
  const isReadOnly = document.body.classList.contains('readonly-mode')
  term.options.disableStdin = isReadOnly
  return { isReadOnly }
}

function attachSession(session, isReadOnly) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return
  if (attachedSession && attachedSession !== session) {
    ws.send(encodeFrame({ session: attachedSession, role: getRole(), type: "detach" }))
  }
  ws.send(encodeFrame({ session, role: getRole(), type: "attach" }))
  attachedSession = session
  requestAnimationFrame(() => {
    fitAddon.fit()
    sendResize()
  })
  if (promptTimer) clearTimeout(promptTimer)
  promptTimer = setTimeout(() => {
    if (!receivedOutput && ws && ws.readyState === WebSocket.OPEN && !isReadOnly) {
      ws.send(encodeFrame({ session, role: "browser", type: "input", data: "\n" }))
    }
  }, 600)
  setStatus("attached")
  setLed(ledWs, "on")
  if (!isReadOnly) btnFiles.disabled = false
  if (!isReadOnly) btnNewShell.disabled = false
  if (!isReadOnly) btnShare.disabled = false
  if (!isReadOnly) btnTunnels.disabled = false
  addShell(session)
  setActiveShell(session)
  // Connect file WS for dedicated file channel (Phase 6)
  if (!isReadOnly) connectFileWs()
  term.focus()
}

function connect() {
  const session = currentSession.trim()
  if (!session) return
  if (!baseSession) baseSession = session
  if (ws && ws.readyState === WebSocket.OPEN) {
    const { isReadOnly } = setupSessionView(session)
    attachSession(session, isReadOnly)
    return
  }
  const target = wsUrl()
  if (ws) {
    try { ws.close() } catch {}
    ws = null
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
    const { isReadOnly } = setupSessionView(session)
    attachSession(session, isReadOnly)
  }
  ws.onmessage = (evt) => {
    const frame = JSON.parse(evt.data)
    if (frame.type === "output" && frame.data) {
      receivedOutput = true
      localEcho = true  // Re-enable local echo after receiving output
      // Hide waiting indicator
      if (waitingForOutput) {
        waitingForOutput = false
        setStatus("attached")
      }
      let chunk = decodeData(frame.data)

      // Local echo matching: skip echoed input to avoid double display
      if (pendingInput && chunk.startsWith(pendingInput)) {
        chunk = chunk.slice(pendingInput.length)
        pendingInput = ""
      }

      if (chunk) {
        term.write(chunk)
        // Suppress terminal query responses generated asynchronously by xterm.js
        suppressInputUntil = Date.now() + 150
        if (frame.session) {
          const prev = shellBuffers.get(frame.session) || ""
          const updated = prev + chunk
          shellBuffers.set(frame.session, updated.length > 131072 ? updated.slice(-131072) : updated)
        }
      }
    } else if (frame.type === "ping") {
      ws.send(encodeFrame({ session: frame.session, role: getRole(), type: "pong" }))
    } else if (frame.type === "file" && frame.data) {
      // Only handle on main WS if file WS is not connected (avoid double processing)
      if (!wsFile || wsFile.readyState !== WebSocket.OPEN) {
        handleFileResponse(frame)
      }
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
    btnShare.disabled = true
    btnTunnels.disabled = true
    attachedSession = ""
  }
  ws.onerror = () => {
      setStatus("error")
      setLed(ledWs, "off")
  }
}

function disconnect() {
  if (!ws) return
  const session = attachedSession || currentSession.trim()
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(encodeFrame({ session, role: getRole(), type: "detach" }))
  }
  ws.close()
  ws = null
  baseSession = ""
  attachedSession = ""
  
  showDashboard()
}

connectBtn.addEventListener("click", connect)
disconnectBtn.addEventListener("click", disconnect)
window.addEventListener("resize", () => { fitAddon.fit(); sendResize() })
term.onData(data => {
  // Suppress terminal query responses (CPR, DA, OSC) generated async by xterm.js after term.write()
  if (Date.now() < suppressInputUntil) return
  const session = currentSession.trim()
  if (!ws || ws.readyState !== WebSocket.OPEN || !session) return
  // Read-only check
  if (document.body.classList.contains('readonly-mode')) return

  // Local echo: immediately display input
  if (localEcho) {
    term.write(data)
    if (session) {
      const prev = shellBuffers.get(session) || ""
      const updated = prev + data
      shellBuffers.set(session, updated.length > 131072 ? updated.slice(-131072) : updated)
    }
    // Track pending input for echo matching
    pendingInput += data
    if (pendingInput.length > 256) pendingInput = pendingInput.slice(-256)
  }

  // Show waiting indicator on Enter
  if (data === '\r' || data === '\n') {
    waitingForOutput = true
    setStatus("⏳ running...")
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
    if (shell.id !== baseSession) {
      const closeBtn = document.createElement("button")
      closeBtn.className = "shell-btn"
      closeBtn.textContent = "✖"
      closeBtn.onclick = (evt) => {
        evt.stopPropagation()
        sendControl(shell.id, { op: "close_shell" })
      }
      actions.appendChild(closeBtn)
    }
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

// Share modal logic
const btnShare = document.getElementById("btn-share")
const shareModal = document.getElementById("share-modal")
const shareResult = document.getElementById("share-result")
const shareUrlInput = document.getElementById("share-url")
const btnGenShare = document.getElementById("btn-gen-share")
const btnCopyShare = document.getElementById("btn-copy-share")

btnShare.addEventListener("click", () => {
  shareResult.style.display = "none"
  shareModal.style.display = "flex"
})

function closeShareModal() {
  shareModal.style.display = "none"
}

btnGenShare.addEventListener("click", async () => {
  const mode = document.querySelector('input[name="share-mode"]:checked').value
  const session = currentSession.trim()
  if (!session) return
  try {
    const url = "/revpty/api/shares" + (secretParam ? "?secret=" + encodeURIComponent(secretParam) : "")
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: session, mode })
    })
    const data = await res.json()
    if (data.url) {
      const full = location.origin + data.url
      shareUrlInput.value = full
      shareResult.style.display = "block"
    } else {
      alert("Error: " + (data.error || "unknown"))
    }
  } catch(e) {
    alert("Share failed: " + e.message)
  }
})

btnCopyShare.addEventListener("click", () => {
  navigator.clipboard.writeText(shareUrlInput.value).then(() => {
    btnCopyShare.textContent = "Copied!"
    setTimeout(() => { btnCopyShare.textContent = "Copy" }, 2000)
  })
})

shareModal.addEventListener("click", (e) => {
  if (e.target === shareModal) closeShareModal()
})

// Tunnel modal logic
const btnTunnels = document.getElementById("btn-tunnels")
const tunnelModal = document.getElementById("tunnel-modal")
const tunnelListEl = document.getElementById("tunnel-list")
const tunnelLocalHost = document.getElementById("tunnel-local-host")
const tunnelLocalPort = document.getElementById("tunnel-local-port")
const btnAddTunnel = document.getElementById("btn-add-tunnel")

btnTunnels.addEventListener("click", () => {
  tunnelModal.style.display = "flex"
  loadTunnels()
})

function closeTunnelModal() {
  tunnelModal.style.display = "none"
}

tunnelModal.addEventListener("click", (e) => {
  if (e.target === tunnelModal) closeTunnelModal()
})

async function loadTunnels() {
  const session = currentSession.trim()
  if (!session) return
  try {
    let url = "/revpty/api/tunnels?session=" + encodeURIComponent(session)
    if (secretParam) url += "&secret=" + encodeURIComponent(secretParam)
    const res = await fetch(url)
    if (!res.ok) throw new Error("Failed to load tunnels")
    const tunnels = await res.json()
    renderTunnelList(tunnels)
  } catch (e) {
    tunnelListEl.innerHTML = '<div class="tunnel-empty" style="color:#ff5555">Error: ' + e.message + '</div>'
  }
}

function renderTunnelList(tunnels) {
  tunnelListEl.innerHTML = ""
  if (tunnels.length === 0) {
    tunnelListEl.innerHTML = '<div class="tunnel-empty">No tunnel mappings. Add one below.</div>'
    return
  }
  tunnels.forEach(t => {
    const div = document.createElement("div")
    div.className = "tunnel-item"
    const tunnelUrl = location.origin + '/' + t.tunnel_id
    div.innerHTML = '<div class="tunnel-info">' +
      '<a class="tunnel-link" href="' + tunnelUrl + '" target="_blank">/' + t.tunnel_id + '</a>' +
      '<span class="tunnel-arrow">&rarr;</span>' +
      '<span class="tunnel-target">' + t.local_host + ':' + t.local_port + '</span>' +
      '</div>'
    const delBtn = document.createElement("button")
    delBtn.className = "btn-sm"
    delBtn.textContent = "Delete"
    delBtn.onclick = () => deleteTunnel(t.tunnel_id)
    div.appendChild(delBtn)
    tunnelListEl.appendChild(div)
  })
}

btnAddTunnel.addEventListener("click", async () => {
  const session = currentSession.trim()
  if (!session) return
  const localHost = tunnelLocalHost.value.trim() || "127.0.0.1"
  const localPort = parseInt(tunnelLocalPort.value)
  if (!localPort) { alert("Service port is required"); return }
  try {
    let url = "/revpty/api/tunnels"
    if (secretParam) url += "?secret=" + encodeURIComponent(secretParam)
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: session, local_host: localHost, local_port: localPort })
    })
    const data = await res.json()
    if (data.error) { alert("Error: " + data.error); return }
    tunnelLocalPort.value = ""
    loadTunnels()
  } catch (e) {
    alert("Add tunnel failed: " + e.message)
  }
})

async function deleteTunnel(tunnelId) {
  try {
    let url = "/revpty/api/tunnels/" + tunnelId
    if (secretParam) url += "?secret=" + encodeURIComponent(secretParam)
    const res = await fetch(url, { method: "DELETE" })
    if (!res.ok) throw new Error("Delete failed")
    loadTunnels()
  } catch (e) {
    alert("Delete tunnel failed: " + e.message)
  }
}

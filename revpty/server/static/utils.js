// Protocol utility functions (pure, no DOM dependencies)

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

// Simple CRC32 for browser
function crc32(buf) {
  let crc = 0xFFFFFFFF
  for (let i = 0; i < buf.length; i++) {
    crc ^= buf[i]
    for (let j = 0; j < 8; j++) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xEDB88320 : 0)
    }
  }
  return (crc ^ 0xFFFFFFFF) >>> 0
}

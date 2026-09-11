const { app, BrowserWindow, ipcMain, dialog, protocol, Menu, shell } = require('electron')
const path = require('path')
const fs = require('fs')
const http = require('http')
const net = require('net')
const { spawn, spawnSync } = require('child_process')

const BACKEND_HOST = '127.0.0.1'
// 既定ポート。使用中なら起動時に空きポートへ自動で切り替える（下の resolvePorts）
const DEFAULT_BACKEND_PORT = 8765
const DEFAULT_LLAMA_TEXT_PORT = 8766
const DEFAULT_LLAMA_VISION_PORT = 8767
let BACKEND_PORT = DEFAULT_BACKEND_PORT
let LLAMA_TEXT_PORT = DEFAULT_LLAMA_TEXT_PORT
let LLAMA_VISION_PORT = DEFAULT_LLAMA_VISION_PORT
const BACKEND_START_TIMEOUT_MS = 30000
const BACKEND_HEALTHCHECK_INTERVAL_MS = 500
const BACKEND_HEALTHCHECK_TIMEOUT_MS = 1000

let backendProcess = null
let isQuitting = false
let isCleaningUpBackend = false
// 起動失敗時の診断用（exit コードと stderr の末尾）
let backendExit = { code: null, signal: null }
const backendStderrTail = []
const BACKEND_STDERR_TAIL_LINES = 20

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// 127.0.0.1 の指定ポートに bind できるか（使用中なら false）
function isPortFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer()
    srv.unref()
    srv.once('error', () => resolve(false))
    srv.listen({ host: BACKEND_HOST, port, exclusive: true }, () => {
      srv.close(() => resolve(true))
    })
  })
}

// start から順に空きポートを探す（exclude は今回すでに割り当てたポート）
async function findFreePort(start, exclude = new Set()) {
  for (let port = start; port < start + 200; port++) {
    if (exclude.has(port)) continue
    if (await isPortFree(port)) return port
  }
  throw new Error(`No free port found from ${start}`)
}

// バックエンド（FastAPI）と llama-server（翻訳用・動画レビュー用）のポートを決める。
// 既定ポートが他プロセス（前回の取り残し・他アプリ）に使われていても起動できるようにする。
// 環境変数 BACKEND_PORT / LLAMA_CPP_PORT / LLAMA_CPP_VISION_PORT で固定指定も可（空き確認はしない）。
async function resolvePorts() {
  const used = new Set()
  const pick = async (envName, def) => {
    const forced = parseInt(process.env[envName] || '', 10)
    const port = Number.isFinite(forced) && forced > 0 ? forced : await findFreePort(def, used)
    used.add(port)
    return port
  }
  BACKEND_PORT = await pick('BACKEND_PORT', DEFAULT_BACKEND_PORT)
  LLAMA_TEXT_PORT = await pick('LLAMA_CPP_PORT', DEFAULT_LLAMA_TEXT_PORT)
  LLAMA_VISION_PORT = await pick('LLAMA_CPP_VISION_PORT', DEFAULT_LLAMA_VISION_PORT)
  const changed = []
  if (BACKEND_PORT !== DEFAULT_BACKEND_PORT) changed.push(`backend ${DEFAULT_BACKEND_PORT}→${BACKEND_PORT}`)
  if (LLAMA_TEXT_PORT !== DEFAULT_LLAMA_TEXT_PORT) changed.push(`llama(text) ${DEFAULT_LLAMA_TEXT_PORT}→${LLAMA_TEXT_PORT}`)
  if (LLAMA_VISION_PORT !== DEFAULT_LLAMA_VISION_PORT) changed.push(`llama(vision) ${DEFAULT_LLAMA_VISION_PORT}→${LLAMA_VISION_PORT}`)
  console.log(`[ports] backend=${BACKEND_PORT} llama-text=${LLAMA_TEXT_PORT} llama-vision=${LLAMA_VISION_PORT}` + (changed.length ? ` (使用中のため変更: ${changed.join(', ')})` : ''))
}

function backendUrl() {
  return `http://${BACKEND_HOST}:${BACKEND_PORT}`
}

function checkBackendHealth() {
  return new Promise((resolve) => {
    const req = http.get(
      {
        host: BACKEND_HOST,
        port: BACKEND_PORT,
        path: '/health',
        timeout: BACKEND_HEALTHCHECK_TIMEOUT_MS,
      },
      (res) => {
        res.resume()
        resolve(res.statusCode === 200)
      }
    )
    req.on('error', () => resolve(false))
    req.on('timeout', () => {
      req.destroy()
      resolve(false)
    })
  })
}

async function waitForBackendReady(timeoutMs) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    // プロセスが既に落ちていれば（.venv 無し・依存不足等）タイムアウトまで待たずに失敗させる
    if (!backendProcess) {
      throw new Error(
        `Backend process exited during startup (code=${backendExit.code}, signal=${backendExit.signal}).\n` +
        (backendStderrTail.join('\n') || '(no stderr output)')
      )
    }
    if (await checkBackendHealth()) {
      return
    }
    await wait(BACKEND_HEALTHCHECK_INTERVAL_MS)
  }
  throw new Error(`Backend did not become ready within ${timeoutMs}ms`)
}

async function stopBackendProcess() {
  if (isCleaningUpBackend) return
  if (!backendProcess || backendProcess.killed) return
  isCleaningUpBackend = true
  const pid = backendProcess.pid
  if (typeof pid !== 'number') {
    backendProcess.kill()
    isCleaningUpBackend = false
    return
  }

  if (process.platform === 'win32') {
    await new Promise((resolve) => {
      const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true })
      killer.on('error', () => resolve())
      killer.on('close', () => resolve())
    })
  } else {
    backendProcess.kill('SIGTERM')
  }
  isCleaningUpBackend = false
}

function stopBackendProcessSync() {
  if (isCleaningUpBackend) return
  if (!backendProcess || backendProcess.killed) return
  isCleaningUpBackend = true
  const pid = backendProcess.pid
  try {
    if (typeof pid !== 'number') {
      backendProcess.kill()
      return
    }
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    } else {
      backendProcess.kill('SIGTERM')
    }
  } catch (_) {
    // best effort cleanup
  } finally {
    isCleaningUpBackend = false
  }
}

async function startBackendProcess() {
  if (backendProcess && !backendProcess.killed) return

  const projectRoot = path.resolve(__dirname, '..')
  const backendEntrypoint = path.join(projectRoot, 'run_backend.py')
  // 優先順: BACKEND_PYTHON 環境変数 → プロジェクト内 .venv → PATH の python
  // （.venv は runtime/python/ に同梱したスタンドアロン CPython から作る。setup_python.bat 参照）
  const venvPython = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
  const pythonCommand =
    process.env.BACKEND_PYTHON || (fs.existsSync(venvPython) ? venvPython : 'python')

  await resolvePorts()
  const env = {
    ...process.env,
    HF_HOME: path.join(projectRoot, 'models'),
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    BACKEND_PORT: String(BACKEND_PORT),
    LLAMA_CPP_PORT: String(LLAMA_TEXT_PORT),
    LLAMA_CPP_VISION_PORT: String(LLAMA_VISION_PORT),
  }

  backendProcess = spawn(pythonCommand, [backendEntrypoint], {
    cwd: projectRoot,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  backendProcess.stdout.setEncoding('utf8')
  backendProcess.stderr.setEncoding('utf8')
  backendProcess.stdout.on('data', (chunk) => {
    console.log(`[backend] ${chunk.trimEnd()}`)
  })
  backendProcess.stderr.on('data', (chunk) => {
    console.error(`[backend] ${chunk.trimEnd()}`)
    for (const line of String(chunk).split(/\r?\n/)) {
      if (!line.trim()) continue
      backendStderrTail.push(line)
      if (backendStderrTail.length > BACKEND_STDERR_TAIL_LINES) backendStderrTail.shift()
    }
  })
  backendProcess.on('exit', (code, signal) => {
    console.log(`[backend] exited (code=${code}, signal=${signal})`)
    backendExit = { code, signal }
    backendProcess = null
    // 稼働中にバックエンドが落ちた場合はレンダラーへ通知（fetch が黙って失敗し続けるのを防ぐ）
    if (!isQuitting) {
      for (const win of BrowserWindow.getAllWindows()) {
        if (!win.isDestroyed()) win.webContents.send('backend:exited', { code, signal })
      }
    }
  })

  await waitForBackendReady(BACKEND_START_TIMEOUT_MS)
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1920,
    height: 1280,
    minWidth: 900,
    minHeight: 650,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    title: 'Video Content Analyzer',
    backgroundColor: '#111111',
    icon: path.join(__dirname, '..', 'assets', 'icon.ico'),
  })
  win.loadFile(path.join(__dirname, 'pages', 'app.html'))

  // モデル出力（動画内容由来）に含まれるリンク等でアプリのウィンドウ自体が外部ページへ
  // 遷移すると、preload の electronAPI（ファイル読込・ごみ箱・場所を開く）が外部ページに
  // 露出する。ページ内遷移はすべて禁止し、http(s) は既定ブラウザで開く
  win.webContents.on('will-navigate', (event, url) => {
    if (url !== win.webContents.getURL()) event.preventDefault()
  })
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isExternalHttpUrl(url)) shell.openExternal(url)
    return { action: 'deny' }
  })

  win.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return
    if ((input.control && input.key === 'r') || input.key === 'F5') {
      win.webContents.reload()
      event.preventDefault()
    }
  })
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null)
  app.setAppUserModelId('com.video-content-analyzer')

  try {
    await startBackendProcess()
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    dialog.showErrorBox('Backend startup failed', message)
    app.quit()
    return
  }

  createMainWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow()
  })
})

app.on('before-quit', async (event) => {
  if (isQuitting) return
  isQuitting = true
  event.preventDefault()
  await stopBackendProcess()
  app.quit()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

process.on('exit', () => {
  stopBackendProcessSync()
})

;['SIGINT', 'SIGTERM', 'SIGHUP'].forEach((signal) => {
  process.on(signal, () => {
    stopBackendProcessSync()
    process.exit(0)
  })
})

// ---------- IPC ハンドラー ----------

// レンダラーから渡されるパスは文字列の絶対パスのみ受け付ける
function isSafeAbsolutePath(p) {
  return typeof p === 'string' && p.length > 0 && p.length < 4096 && path.isAbsolute(p)
}
function isExternalHttpUrl(url) {
  try {
    const u = new URL(String(url))
    return u.protocol === 'http:' || u.protocol === 'https:'
  } catch (_) {
    return false
  }
}

// レンダラーがバックエンドの接続先を知るため（preload が起動時に同期取得）
ipcMain.on('backend:url', (event) => {
  event.returnValue = backendUrl()
})

// 動画ファイルを開くダイアログ
ipcMain.handle('dialog:openVideo', async () => {
  const { filePaths } = await dialog.showOpenDialog({
    title: '動画ファイルを選択',
    filters: [
      { name: '動画', extensions: ['mp4', 'mkv', 'avi', 'mov', 'webm', 'm4v', 'flv'] },
      { name: 'すべてのファイル', extensions: ['*'] },
    ],
    properties: ['openFile'],
  })
  return filePaths[0] ?? null
})

// ルートフォルダを選択するダイアログ（ファイル一覧用）
ipcMain.handle('dialog:openFolder', async () => {
  const { filePaths } = await dialog.showOpenDialog({
    title: 'ルートフォルダを選択',
    properties: ['openDirectory'],
  })
  return filePaths[0] ?? null
})

// SRT ファイルを開くダイアログ
ipcMain.handle('dialog:openSrt', async () => {
  const { filePaths } = await dialog.showOpenDialog({
    title: 'SRT ファイルを選択',
    filters: [{ name: '字幕', extensions: ['srt', 'vtt'] }],
    properties: ['openFile'],
  })
  return filePaths[0] ?? null
})

// ファイル/フォルダを OS のごみ箱に移動（完全削除はしない）
ipcMain.handle('fs:trashItem', async (_, filePath) => {
  if (!isSafeAbsolutePath(filePath)) return { ok: false, error: 'invalid path' }
  try {
    await shell.trashItem(path.normalize(filePath))
    return { ok: true }
  } catch (e) {
    return { ok: false, error: e.message }
  }
})

// エクスプローラーでファイル/フォルダの場所を開く（項目を選択状態で表示）
ipcMain.handle('fs:showItemInFolder', (_, filePath) => {
  if (!isSafeAbsolutePath(filePath)) return
  shell.showItemInFolder(path.normalize(filePath))
})

// http(s) の URL を既定ブラウザで開く（チャット回答内のリンク用）
ipcMain.handle('shell:openExternal', (_, url) => {
  if (!isExternalHttpUrl(url)) return { ok: false, error: 'invalid url' }
  shell.openExternal(String(url))
  return { ok: true }
})

// テキストファイルを読み込む（SRT / VTT 読み込み専用。それ以外の拡張子は拒否）
const READABLE_TEXT_EXTS = new Set(['.srt', '.vtt'])
const READ_FILE_MAX_BYTES = 50 * 1024 * 1024
ipcMain.handle('fs:readFile', (_, filePath) => {
  if (!isSafeAbsolutePath(filePath) || !READABLE_TEXT_EXTS.has(path.extname(filePath).toLowerCase())) {
    return { ok: false, error: 'invalid path' }
  }
  try {
    const st = fs.statSync(filePath)
    if (!st.isFile()) return { ok: false, error: 'not a file' }
    if (st.size > READ_FILE_MAX_BYTES) return { ok: false, error: 'file too large' }
    return { ok: true, content: fs.readFileSync(filePath, 'utf-8') }
  } catch (e) {
    return { ok: false, error: e.message }
  }
})

const { app, BrowserWindow, ipcMain, dialog, protocol, Menu } = require('electron')
const path = require('path')
const fs = require('fs')
const http = require('http')
const { spawn, spawnSync } = require('child_process')

const BACKEND_HOST = '127.0.0.1'
const BACKEND_PORT = 8765
const BACKEND_START_TIMEOUT_MS = 30000
const BACKEND_HEALTHCHECK_INTERVAL_MS = 500
const BACKEND_HEALTHCHECK_TIMEOUT_MS = 1000

let backendProcess = null
let isQuitting = false
let isCleaningUpBackend = false

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
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
  const pythonCommand = process.env.BACKEND_PYTHON || 'python'

  const env = {
    ...process.env,
    HF_HOME: path.join(projectRoot, 'models'),
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
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
  })
  backendProcess.on('exit', (code, signal) => {
    console.log(`[backend] exited (code=${code}, signal=${signal})`)
    backendProcess = null
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

// SRT ファイルを開くダイアログ
ipcMain.handle('dialog:openSrt', async () => {
  const { filePaths } = await dialog.showOpenDialog({
    title: 'SRT ファイルを選択',
    filters: [{ name: '字幕', extensions: ['srt', 'vtt'] }],
    properties: ['openFile'],
  })
  return filePaths[0] ?? null
})

// テキストファイルを読み込む（SRT 読み込み用）
ipcMain.handle('fs:readFile', (_, filePath) => {
  try {
    return { ok: true, content: fs.readFileSync(filePath, 'utf-8') }
  } catch (e) {
    return { ok: false, error: e.message }
  }
})

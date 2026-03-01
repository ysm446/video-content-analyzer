const { app, BrowserWindow, ipcMain, dialog, protocol, Menu } = require('electron')
const path = require('path')
const fs = require('fs')

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
    title: 'Movie Review',
    backgroundColor: '#111111',
  })
  win.loadFile(path.join(__dirname, 'pages', 'app.html'))
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null)
  createMainWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
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

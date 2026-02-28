const { contextBridge, ipcRenderer, webUtils } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // ファイルダイアログ
  openVideo: ()         => ipcRenderer.invoke('dialog:openVideo'),
  openSrt:   ()         => ipcRenderer.invoke('dialog:openSrt'),

  // ファイル読み込み
  readFile:  (filePath) => ipcRenderer.invoke('fs:readFile', filePath),

  // ドラッグ&ドロップされた File オブジェクトから絶対パスを取得
  // (Electron 32+ で file.path が sandbox 環境で使えなくなったための代替)
  getPathForFile: (file) => webUtils.getPathForFile(file),
})
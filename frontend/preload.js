const { contextBridge, ipcRenderer, webUtils } = require('electron')

// バックエンドの接続先（ポートは使用中なら起動時に変わるため main から受け取る）
const backendUrl = ipcRenderer.sendSync('backend:url')

contextBridge.exposeInMainWorld('electronAPI', {
  backendUrl,

  // ファイルダイアログ
  openVideo: ()         => ipcRenderer.invoke('dialog:openVideo'),
  openFolder: ()        => ipcRenderer.invoke('dialog:openFolder'),
  openSrt:   ()         => ipcRenderer.invoke('dialog:openSrt'),

  // ファイル読み込み
  readFile:  (filePath) => ipcRenderer.invoke('fs:readFile', filePath),

  // ごみ箱に移動
  trashItem: (filePath) => ipcRenderer.invoke('fs:trashItem', filePath),

  // エクスプローラーで場所を開く（項目を選択状態で表示）
  showItemInFolder: (filePath) => ipcRenderer.invoke('fs:showItemInFolder', filePath),

  // ドラッグ&ドロップされた File オブジェクトから絶対パスを取得
  // (Electron 32+ で file.path が sandbox 環境で使えなくなったための代替)
  getPathForFile: (file) => webUtils.getPathForFile(file),
})
const { contextBridge, ipcRenderer, webUtils } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
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
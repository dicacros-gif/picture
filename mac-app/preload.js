const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("picture", {
  saveImages: images => ipcRenderer.invoke("save-images", images),
  collectKeywords: seed => ipcRenderer.invoke("collect-keywords", seed),
  openNaverLogin: blogId => ipcRenderer.invoke("open-naver-login", blogId),
  openBlogWrite: () => ipcRenderer.invoke("open-blog-write"),
  replyComments: options => ipcRenderer.invoke("reply-comments", options),
  getSettings: () => ipcRenderer.invoke("get-settings"),
  setSettings: settings => ipcRenderer.invoke("set-settings", settings),
  onReplyProgress: callback => ipcRenderer.on("reply-progress", (_e, value) => callback(value))
});

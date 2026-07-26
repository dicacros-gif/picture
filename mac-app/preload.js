const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("picture", {
  saveImages: images => ipcRenderer.invoke("save-images", images),
  collectRealtime: () => ipcRenderer.invoke("collect-realtime"),
  collectKeywords: seed => ipcRenderer.invoke("collect-keywords", seed),
  googleImageSearch: keyword => ipcRenderer.invoke("google-image-search", keyword),
  openNaverLogin: blogId => ipcRenderer.invoke("open-naver-login", blogId),
  openBlogWrite: () => ipcRenderer.invoke("open-blog-write"),
  replyComments: options => ipcRenderer.invoke("reply-comments", options),
  heartRecentPosts: options => ipcRenderer.invoke("heart-recent-posts", options),
  commentNeighborFeed: options => ipcRenderer.invoke("comment-neighbor-feed", options),
  stopNeighborComments: () => ipcRenderer.invoke("stop-neighbor-comments"),
  getSettings: () => ipcRenderer.invoke("get-settings"),
  setSettings: settings => ipcRenderer.invoke("set-settings", settings),
  onReplyProgress: callback => ipcRenderer.on("reply-progress", (_e, value) => callback(value)),
  onHeartProgress: callback => ipcRenderer.on("heart-progress", (_e, value) => callback(value)),
  onNeighborProgress: callback => ipcRenderer.on("neighbor-progress", (_e, value) => callback(value))
});

const { app, BrowserWindow, ipcMain, dialog, shell, session } = require("electron");
const fs = require("fs");
const path = require("path");

let mainWindow;
let naverWindow;

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const userDataFile = name => path.join(app.getPath("userData"), name);

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 940,
    minHeight: 680,
    titleBarStyle: "hiddenInset",
    backgroundColor: "#f4f7fb",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
}

function getNaverWindow() {
  if (naverWindow && !naverWindow.isDestroyed()) return naverWindow;
  naverWindow = new BrowserWindow({
    width: 1180,
    height: 860,
    title: "네이버 로그인 및 자동화",
    webPreferences: {
      partition: "persist:naver-picture-cleaner",
      contextIsolation: true,
      sandbox: true
    }
  });
  naverWindow.on("closed", () => { naverWindow = null; });
  naverWindow.webContents.setWindowOpenHandler(({ url }) => {
    naverWindow.loadURL(url);
    return { action: "deny" };
  });
  return naverWindow;
}

async function fetchText(url) {
  const response = await fetch(url, {
    headers: { "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X 14_5) AppleWebKit/537.36 Chrome/126 Safari/537.36" }
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.text();
}

function unique(items) {
  const seen = new Set();
  return items.map(x => String(x || "").replace(/<[^>]+>/g, "").replace(/&[a-z]+;/gi, " ").trim())
    .filter(x => x.length >= 2 && x.length <= 45 && !seen.has(x) && seen.add(x));
}

ipcMain.handle("save-images", async (_event, images) => {
  const picked = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory", "createDirectory"] });
  if (picked.canceled) return { canceled: true };
  for (const image of images) {
    const base64 = image.dataUrl.replace(/^data:image\/jpeg;base64,/, "");
    fs.writeFileSync(path.join(picked.filePaths[0], image.name), Buffer.from(base64, "base64"));
  }
  return { canceled: false, folder: picked.filePaths[0], count: images.length };
});

ipcMain.handle("collect-keywords", async (_event, seed) => {
  const q = encodeURIComponent((seed || "").trim());
  const endpoints = [
    ["네이버", `https://ac.search.naver.com/nx/ac?q=${q}&con=0&frm=nv&ans=2&r_format=json&r_enc=UTF-8&r_unicode=0&t_koreng=1&run=2&rev=4`],
    ["다음", `https://suggest.search.daum.net/sushi/pc/get?q=${q}`],
    ["구글", `https://suggestqueries.google.com/complete/search?client=firefox&q=${q}`]
  ];
  const output = [];
  for (const [source, url] of endpoints) {
    try {
      const text = await fetchText(url);
      const matches = text.match(/"([^"\\]{2,50})"/g) || [];
      output.push(...unique(matches.map(x => x.slice(1, -1))).map(keyword => ({ source, keyword })));
    } catch (error) {
      output.push({ source, keyword: `수집 실패: ${error.message}`, error: true });
    }
  }
  return output.slice(0, 40);
});

ipcMain.handle("open-naver-login", async (_event, blogId) => {
  const win = getNaverWindow();
  await win.loadURL(blogId ? `https://blog.naver.com/${encodeURIComponent(blogId)}` : "https://nid.naver.com/nidlogin.login");
  win.show();
  return true;
});

ipcMain.handle("open-blog-write", async () => {
  const win = getNaverWindow();
  await win.loadURL("https://blog.naver.com/GoBlogWrite.naver");
  win.show();
  return true;
});

ipcMain.handle("get-settings", () => {
  try { return JSON.parse(fs.readFileSync(userDataFile("settings.json"), "utf8")); }
  catch { return {}; }
});

ipcMain.handle("set-settings", (_event, settings) => {
  fs.writeFileSync(userDataFile("settings.json"), JSON.stringify(settings, null, 2));
  return true;
});

ipcMain.handle("reply-comments", async (event, options) => {
  const blogId = String(options.blogId || "").trim();
  const phrases = unique(options.phrases || []);
  if (!blogId) throw new Error("네이버 블로그 아이디를 입력해 주세요.");
  if (!phrases.length) throw new Error("감사 문구를 한 개 이상 입력해 주세요.");

  const win = getNaverWindow();
  win.show();
  const send = payload => event.sender.send("reply-progress", payload);
  const processedPath = userDataFile("replied-comments.json");
  let processed = {};
  try { processed = JSON.parse(fs.readFileSync(processedPath, "utf8")); } catch {}

  send({ status: "최근 글 목록을 확인합니다.", done: 0, skipped: 0 });
  await win.loadURL(`https://blog.naver.com/PostList.naver?blogId=${encodeURIComponent(blogId)}&from=postList`);
  await wait(2500);

  let posts = [];
  for (const frame of win.webContents.mainFrame.framesInSubtree) {
    const found = await frame.executeJavaScript(`(() => {
    const cutoff = Date.now() - 10 * 86400000;
    const links = [...document.querySelectorAll('a[href*="logNo="], a[href*="/${blogId}/"]')];
    const out = [];
    for (const a of links) {
      const href = a.href || "";
      const m = href.match(/(?:logNo=|\\/${blogId}\\/)(\\d+)/);
      if (!m || out.some(x => x.logNo === m[1])) continue;
      const box = a.closest('li, article, .post, .blog2_series') || a.parentElement;
      const text = box ? box.innerText : a.innerText;
      const dm = text.match(/(20\\d{2})[.\\/-]\\s*(\\d{1,2})[.\\/-]\\s*(\\d{1,2})/);
      let date = dm ? new Date(+dm[1], +dm[2] - 1, +dm[3]).getTime() : Date.now();
      if (date >= cutoff) out.push({ logNo: m[1], title: (a.innerText || '').trim(), date });
    }
    return out.slice(0, 100);
    })()`, true).catch(() => []);
    posts.push(...found);
  }
  posts = posts.filter((post, index, all) => all.findIndex(x => x.logNo === post.logNo) === index)
    .sort((a, b) => b.date - a.date);

  let done = 0, skipped = 0, failed = 0;
  for (const post of posts) {
    send({ status: `글 확인: ${post.title || post.logNo}`, done, skipped, failed });
    await win.loadURL(`https://blog.naver.com/PostView.naver?blogId=${encodeURIComponent(blogId)}&logNo=${post.logNo}`);
    await wait(2200);
    const frames = win.webContents.mainFrame.framesInSubtree;
    const target = frames.find(f => /PostView|blog\.naver\.com/.test(f.url)) || win.webContents.mainFrame;
    const comments = await target.executeJavaScript(`(() => {
      const clickText = text => [...document.querySelectorAll('button,a')].find(e => (e.innerText||'').includes(text));
      const toggle = clickText('댓글');
      if (toggle) toggle.click();
      return true;
    })()`, true).catch(() => false);
    await wait(1500);

    const result = await target.executeJavaScript(`(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const blogId = ${JSON.stringify(blogId)};
      const phrases = ${JSON.stringify(phrases)};
      const processed = ${JSON.stringify(processed)};
      const logNo = ${JSON.stringify(post.logNo)};
      const blocks = [...document.querySelectorAll('[class*="comment_item"], [class*="u_cbox_comment_box"], li[class*="comment"]')];
      let done = 0, skipped = 0, failed = 0;
      for (let i = 0; i < blocks.length; i++) {
        const block = blocks[i];
        const author = (block.querySelector('[class*="name"], [class*="nick"]')?.innerText || '').trim();
        const body = (block.querySelector('[class*="text"], [class*="contents"]')?.innerText || '').trim();
        const key = logNo + ':' + author + ':' + body.slice(0, 80);
        if (!body || author === blogId || processed[key]) { skipped++; continue; }
        const ownReply = [...block.querySelectorAll('[class*="reply"], [class*="comment"]')]
          .some(e => (e.querySelector('[class*="name"],[class*="nick"]')?.innerText || '').includes(blogId));
        if (ownReply) { skipped++; continue; }
        const reply = [...block.querySelectorAll('button,a')].find(e => /답글|답변/.test(e.innerText || ''));
        if (!reply) { failed++; continue; }
        reply.click(); await sleep(350);
        const editor = block.querySelector('textarea,[contenteditable="true"]') ||
          document.querySelector('textarea:focus,[contenteditable="true"]:focus');
        if (!editor) { failed++; continue; }
        const phrase = phrases[Math.floor(Math.random() * phrases.length)];
        editor.focus();
        if (editor.tagName === 'TEXTAREA') {
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          setter.call(editor, phrase); editor.dispatchEvent(new Event('input', {bubbles:true}));
        } else {
          editor.textContent = phrase; editor.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:phrase}));
        }
        const submit = [...block.querySelectorAll('button,a')].find(e => /등록|확인/.test(e.innerText || '') && !e.disabled);
        if (!submit) { failed++; continue; }
        submit.click(); await sleep(700);
        processed[key] = { at: new Date().toISOString(), phrase };
        done++;
      }
      return { done, skipped, failed, processed, found: blocks.length };
    })()`, true).catch(error => ({ done: 0, skipped: 0, failed: 1, error: error.message, processed, found: 0 }));
    done += result.done || 0; skipped += result.skipped || 0; failed += result.failed || 0;
    processed = result.processed || processed;
    fs.writeFileSync(processedPath, JSON.stringify(processed, null, 2));
    send({ status: `${post.title || post.logNo} 완료`, done, skipped, failed });
    await wait(700);
  }
  const summary = { posts: posts.length, done, skipped, failed };
  send({ status: "작업 완료", ...summary, complete: true });
  return summary;
});

app.whenReady().then(() => {
  createMainWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createMainWindow(); });
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });

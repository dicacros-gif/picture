const { app, BrowserWindow, ipcMain, dialog, shell, session } = require("electron");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

let mainWindow;
let naverWindow;
let neighborJobCancelled = false;
let whaleProcess;
const WHALE_DEBUG_PORT = 9339;

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const userDataFile = name => path.join(app.getPath("userData"), name);

class CdpPage {
  constructor(webSocketUrl) {
    this.nextId = 1;
    this.pending = new Map();
    this.socket = new WebSocket(webSocketUrl);
    this.ready = new Promise((resolve, reject) => {
      this.socket.onopen = resolve;
      this.socket.onerror = () => reject(new Error("네이버 웨일 자동화 연결에 실패했습니다."));
    });
    this.socket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message)); else resolve(message.result || {});
    };
  }
  async send(method, params = {}) {
    await this.ready;
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async initialize() {
    await this.send("Page.enable");
    await this.send("Runtime.enable");
    await this.send("Network.enable");
  }
  async navigate(url) {
    await this.send("Page.navigate", { url });
    await wait(2200);
  }
  async evaluateFrames(expression) {
    const tree = await this.send("Page.getFrameTree");
    const ids = [];
    const visit = node => {
      if (node?.frame?.id) ids.push(node.frame.id);
      for (const child of node?.childFrames || []) visit(child);
    };
    visit(tree.frameTree);
    const results = [];
    for (const frameId of ids) {
      try {
        const world = await this.send("Page.createIsolatedWorld", {
          frameId, worldName: `picture-cleaner-${Date.now()}-${frameId}`, grantUniversalAccess: true
        });
        const evaluated = await this.send("Runtime.evaluate", {
          expression, contextId: world.executionContextId,
          awaitPromise: true, returnByValue: true, userGesture: true
        });
        if (!evaluated.exceptionDetails) results.push(evaluated.result?.value);
      } catch {}
    }
    return results;
  }
  disconnect() {
    try { this.socket.close(); } catch {}
  }
}

function whaleExecutable() {
  const candidates = [
    "/Applications/Naver Whale.app/Contents/MacOS/Naver Whale",
    path.join(app.getPath("home"), "Applications/Naver Whale.app/Contents/MacOS/Naver Whale")
  ];
  return candidates.find(candidate => fs.existsSync(candidate));
}

function whaleDefaultProfile() {
  const root = path.join(app.getPath("home"), "Library/Application Support/Naver/Whale");
  if (!fs.existsSync(root)) return null;
  let profileName = "Default";
  try {
    const localState = JSON.parse(fs.readFileSync(path.join(root, "Local State"), "utf8"));
    profileName = localState?.profile?.last_used || "Default";
  } catch {}
  const profile = path.join(root, profileName);
  return fs.existsSync(profile) ? { root, profile } : null;
}

function copyWhaleSession(source, destination) {
  if (!fs.existsSync(source)) return;
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  try {
    const sourceStat = fs.statSync(source);
    if (sourceStat.isDirectory()) {
      fs.cpSync(source, destination, { recursive: true, force: true });
    } else {
      fs.copyFileSync(source, destination);
    }
  } catch {}
}

function syncExistingWhaleLogin(targetRoot) {
  const source = whaleDefaultProfile();
  if (!source) return false;
  const targetProfile = path.join(targetRoot, "Default");
  fs.mkdirSync(targetProfile, { recursive: true });
  copyWhaleSession(path.join(source.root, "Local State"), path.join(targetRoot, "Local State"));
  const items = [
    "Cookies", "Cookies-wal", "Cookies-shm",
    "Network/Cookies", "Network/Cookies-wal", "Network/Cookies-shm",
    "Local Storage", "Session Storage", "IndexedDB", "WebStorage",
    "Preferences", "Secure Preferences", "Web Data", "Login Data"
  ];
  for (const item of items) {
    copyWhaleSession(path.join(source.profile, item), path.join(targetProfile, item));
  }
  return true;
}

async function ensureWhale() {
  if (process.platform !== "darwin") return null;
  const executable = whaleExecutable();
  if (!executable) throw new Error("네이버 웨일이 설치되어 있지 않습니다. 웨일을 설치한 뒤 다시 실행해 주세요.");
  const versionUrl = `http://127.0.0.1:${WHALE_DEBUG_PORT}/json/version`;
  try {
    const response = await fetch(versionUrl);
    if (response.ok) return;
  } catch {}
  const profile = path.join(app.getPath("userData"), "naver-whale-profile");
  syncExistingWhaleLogin(profile);
  whaleProcess = spawn(executable, [
    `--remote-debugging-port=${WHALE_DEBUG_PORT}`,
    "--remote-allow-origins=*",
    `--user-data-dir=${profile}`,
    "--profile-directory=Default",
    "--start-maximized",
    "--no-first-run",
    "--disable-notifications",
    "about:blank"
  ], { detached: false, stdio: "ignore" });
  for (let attempt = 0; attempt < 50; attempt++) {
    try {
      const response = await fetch(versionUrl);
      if (response.ok) return;
    } catch {}
    await wait(250);
  }
  throw new Error("네이버 웨일 자동화 연결을 시작하지 못했습니다.");
}

async function openWhalePage(url) {
  await ensureWhale();
  const response = await fetch(
    `http://127.0.0.1:${WHALE_DEBUG_PORT}/json/new?${encodeURIComponent(url)}`,
    { method: "PUT" }
  );
  if (!response.ok) throw new Error("네이버 웨일 탭을 열지 못했습니다.");
  const target = await response.json();
  const page = new CdpPage(target.webSocketDebuggerUrl);
  await page.initialize();
  await wait(1800);
  return page;
}

async function requireNaverWhaleLogin(page) {
  try {
    const result = await page.send("Storage.getCookies");
    const loggedIn = (result.cookies || []).some(cookie =>
      /(^|\.)naver\.com$/i.test(cookie.domain || "") &&
      (cookie.name === "NID_SES" || cookie.name === "NID_AUT") &&
      Boolean(cookie.value)
    );
    if (loggedIn) return true;
  } catch {}
  await page.navigate("https://nid.naver.com/nidlogin.login");
  throw new Error(
    "네이버 로그인이 확인되지 않아 웨일 로그인 화면을 열었습니다. 로그인 후 작업 버튼을 다시 눌러 주세요."
  );
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 940,
    minHeight: 680,
    fullscreen: process.platform === "darwin",
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
  const bytes = await response.arrayBuffer();
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  const encoding = contentType.includes("euc-kr") ? "euc-kr" : "utf-8";
  return new TextDecoder(encoding).decode(bytes);
}

function unique(items) {
  const seen = new Set();
  return items.map(x => String(x || "").normalize("NFC")
    .replace(/<[^>]+>/g, "").replace(/&[a-z]+;/gi, " ").trim())
    .filter(x => x.length >= 2 && x.length <= 45 && !seen.has(x) && seen.add(x));
}

async function waitForPage(win, predicate, timeout = 18000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const ready = await win.webContents.executeJavaScript(predicate, true).catch(() => false);
    if (ready) return true;
    await wait(500);
  }
  return false;
}

ipcMain.handle("collect-realtime", async () => {
  const collector = new BrowserWindow({
    show: false,
    width: 1400,
    height: 1100,
    webPreferences: { contextIsolation: true, sandbox: true }
  });
  const output = {
    "다음": [],
    "구글": [],
    "크리에이터 어드바이저": [],
    "네이버 시그널": []
  };
  try {
    await collector.loadURL("https://adsensefarm.kr/realtime");
    await waitForPage(collector, `(() => [...document.querySelectorAll('.item .kwds .keyword')]
      .some(e => (e.innerText||'').trim() && (e.innerText||'').trim() !== '-'))()`);
    const adsense = await collector.webContents.executeJavaScript(`(() => {
      const defs = [
        {source:'다음', titles:['다음 실시간 검색어']},
        {source:'구글', titles:['구글 실시간 검색어']},
        {source:'크리에이터 어드바이저', titles:['크리에이터 어드바이저 검색어','네이버 실시간 검색어']}
      ];
      const result = {};
      const cards = [...document.querySelectorAll('.item')];
      for (const def of defs) {
        const card = cards.find(c => def.titles.includes((c.querySelector('h2')?.innerText||'').trim()));
        result[def.source] = card ? [...card.querySelectorAll('.kwds .keyword')]
          .map(e => (e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim())
          .filter(x => x && x !== '-').slice(0,10) : [];
      }
      return result;
    })()`, true);
    for (const [source, values] of Object.entries(adsense || {})) output[source] = unique(values).slice(0, 10);

    await collector.loadURL("https://www.signal.bz/");
    await waitForPage(collector, `document.querySelectorAll('.realtime-rank .rank-text,.rank-text').length > 0`);
    const signal = await collector.webContents.executeJavaScript(`(() =>
      [...document.querySelectorAll('.realtime-rank .rank-text,.rank-text')]
        .map(e => (e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim())
        .filter(Boolean).slice(0,10))()`, true);
    output["네이버 시그널"] = unique(signal).slice(0, 10);
  } finally {
    if (!collector.isDestroyed()) collector.destroy();
  }
  return output;
});

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
    ["다음", `https://suggest.search.daum.net/sushi/opensearch/pc?q=${q}&DA=JU2`],
    ["구글", `https://suggestqueries.google.com/complete/search?client=firefox&q=${q}`]
  ];
  const output = [];
  const globallySeen = new Set([String(seed || "").trim().toLocaleLowerCase("ko-KR")]);
  for (const [source, url] of endpoints) {
    try {
      const text = await fetchText(url);
      const data = JSON.parse(text);
      let values = [];
      if (source === "네이버") {
        for (const group of data.items || []) {
          for (const item of group || []) if (Array.isArray(item) && item[0]) values.push(String(item[0]));
        }
      } else if (source === "다음") {
        values = Array.isArray(data?.[1]) ? data[1] : [];
      } else {
        values = Array.isArray(data?.[1]) ? data[1] : [];
      }
      for (const keyword of unique(values)) {
        const key = keyword.toLocaleLowerCase("ko-KR");
        if (!globallySeen.has(key)) {
          globallySeen.add(key);
          output.push({ source, keyword });
        }
      }
    } catch (error) {
      output.push({ source, error: true, message: error.message });
    }
  }
  return output.slice(0, 40);
});

ipcMain.handle("google-image-search", async (_event, keyword) => {
  const korean = unique([keyword])[0];
  if (!korean) throw new Error("1번에서 연관 검색어를 먼저 선택해 주세요.");
  const endpoint = "https://translate.googleapis.com/translate_a/single"
    + `?client=gtx&sl=ko&tl=en&dt=t&q=${encodeURIComponent(korean)}`;
  const translatedData = JSON.parse(await fetchText(endpoint));
  const english = (translatedData?.[0] || []).map(part => part?.[0] || "").join("").trim();
  if (!english) throw new Error("영어 번역 결과를 가져오지 못했습니다.");
  const imageUrl = `https://www.google.com/search?tbm=isch&q=${encodeURIComponent(english)}`;
  if (process.platform === "darwin") {
    const page = await openWhalePage(imageUrl);
    page.disconnect();
  } else {
    await shell.openExternal(imageUrl);
  }
  return { korean, english, imageUrl };
});

ipcMain.handle("open-naver-login", async (_event, blogId) => {
  if (process.platform === "darwin") {
    await openWhalePage(blogId
      ? `https://blog.naver.com/${encodeURIComponent(blogId)}`
      : "https://nid.naver.com/nidlogin.login");
    return true;
  }
  const win = getNaverWindow();
  await win.loadURL(blogId ? `https://blog.naver.com/${encodeURIComponent(blogId)}` : "https://nid.naver.com/nidlogin.login");
  win.show();
  return true;
});

ipcMain.handle("open-blog-write", async () => {
  if (process.platform === "darwin") {
    await openWhalePage("https://blog.naver.com/GoBlogWrite.naver");
    return true;
  }
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

async function recentWhalePosts(page, blogId) {
  await page.navigate(`https://blog.naver.com/PostList.naver?blogId=${encodeURIComponent(blogId)}&from=postList`);
  const values = await page.evaluateFrames(`(() => {
    const cutoff = Date.now() - 10 * 86400000;
    const links = [...document.querySelectorAll('a[href*="logNo="],a[href*="/${blogId}/"]')];
    const out = [];
    for (const a of links) {
      const m = (a.href||'').match(/(?:logNo=|\\/${blogId}\\/)(\\d+)/);
      if (!m || out.some(x => x.logNo === m[1])) continue;
      const box = a.closest('li,article,.post') || a.parentElement;
      const dm = (box?.innerText||'').match(/(20\\d{2})[.\\/-]\\s*(\\d{1,2})[.\\/-]\\s*(\\d{1,2})/);
      const date = dm ? new Date(+dm[1],+dm[2]-1,+dm[3]).getTime() : Date.now();
      if (date >= cutoff) out.push({logNo:m[1],title:(a.innerText||'').trim(),date});
    }
    return out;
  })()`);
  const posts = values.flatMap(value => Array.isArray(value) ? value : []);
  return posts.filter((post, index, all) => all.findIndex(x => x.logNo === post.logNo) === index)
    .sort((a, b) => b.date - a.date);
}

async function replyCommentsInWhale(event, options) {
  const blogId = String(options.blogId || "dicajohn").trim();
  const phrases = unique(options.phrases || []);
  if (!phrases.length) throw new Error("감사 문구를 한 개 이상 입력해 주세요.");
  const send = payload => event.sender.send("reply-progress", payload);
  const processedPath = userDataFile("replied-comments.json");
  let processed = {};
  try { processed = JSON.parse(fs.readFileSync(processedPath, "utf8")); } catch {}
  const page = await openWhalePage("about:blank");
  try {
    await requireNaverWhaleLogin(page);
    const posts = await recentWhalePosts(page, blogId);
    let done = 0, skipped = 0, failed = 0;
    for (const post of posts) {
      send({ status: `웨일에서 글 확인: ${post.title || post.logNo}`, done, skipped, failed });
      await page.navigate(`https://blog.naver.com/PostView.naver?blogId=${encodeURIComponent(blogId)}&logNo=${post.logNo}`);
      await Promise.all((await page.evaluateFrames(`(() => {
        const toggle=[...document.querySelectorAll('button,a')].find(e=>(e.innerText||'').includes('댓글'));
        if(toggle)toggle.click(); return true;
      })()`)).map(() => Promise.resolve()));
      await wait(900);
      const results = await page.evaluateFrames(`(async () => {
        const sleep=ms=>new Promise(r=>setTimeout(r,ms));
        const blogId=${JSON.stringify(blogId)}, phrases=${JSON.stringify(phrases)};
        const processed=${JSON.stringify(processed)}, logNo=${JSON.stringify(post.logNo)};
        const blocks=[...document.querySelectorAll('[class*="comment_item"],[class*="u_cbox_comment_box"],li[class*="comment"]')];
        let done=0,skipped=0,failed=0;
        for(const block of blocks){
          const author=(block.querySelector('[class*="name"],[class*="nick"]')?.innerText||'').trim();
          const body=(block.querySelector('[class*="text"],[class*="contents"]')?.innerText||'').trim();
          const key=logNo+':'+author+':'+body.slice(0,80);
          if(!body||author===blogId||processed[key]){skipped++;continue;}
          const own=[...block.querySelectorAll('[class*="reply"],[class*="comment"]')]
            .some(e=>(e.querySelector('[class*="name"],[class*="nick"]')?.innerText||'').includes(blogId));
          if(own){skipped++;continue;}
          const reply=[...block.querySelectorAll('button,a')].find(e=>/답글|답변/.test(e.innerText||''));
          if(!reply){failed++;continue;} reply.click(); await sleep(300);
          const editor=block.querySelector('textarea,[contenteditable="true"]')||document.querySelector('textarea:focus,[contenteditable="true"]:focus');
          if(!editor){failed++;continue;}
          const phrase=phrases[Math.floor(Math.random()*phrases.length)]; editor.focus();
          if(editor.tagName==='TEXTAREA'){
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor,phrase);
            editor.dispatchEvent(new Event('input',{bubbles:true}));
          }else{editor.textContent=phrase;editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:phrase}));}
          const submit=[...block.querySelectorAll('button,a')].find(e=>/등록|확인/.test(e.innerText||'')&&!e.disabled);
          if(!submit){failed++;continue;} submit.click(); await sleep(650);
          processed[key]={at:new Date().toISOString(),phrase};done++;
        }
        return {done,skipped,failed,processed,found:blocks.length};
      })()`);
      const result = results.find(value => value?.found) || { done: 0, skipped: 0, failed: 0, processed };
      done += result.done || 0; skipped += result.skipped || 0; failed += result.failed || 0;
      processed = result.processed || processed;
      fs.writeFileSync(processedPath, JSON.stringify(processed, null, 2));
    }
    const summary = { posts: posts.length, done, skipped, failed };
    send({ status: "웨일 댓글 답글 완료", ...summary, complete: true });
    return summary;
  } finally { page.disconnect(); }
}

async function heartRecentInWhale(event, options) {
  const blogId = String(options.blogId || "dicajohn").trim();
  const send = payload => event.sender.send("heart-progress", payload);
  const page = await openWhalePage("about:blank");
  try {
    await requireNaverWhaleLogin(page);
    const posts = await recentWhalePosts(page, blogId);
    let hearted = 0, skipped = 0, failed = 0;
    for (const post of posts) {
      send({ status: `웨일 공감 확인: ${post.title || post.logNo}`, hearted, skipped, failed });
      await page.navigate(`https://blog.naver.com/PostView.naver?blogId=${encodeURIComponent(blogId)}&logNo=${post.logNo}`);
      const results = await page.evaluateFrames(`(async () => {
        const candidates=[...document.querySelectorAll('button,a')].filter(e=>/공감|좋아요/.test((e.innerText||'')+' '+(e.getAttribute('aria-label')||'')));
        const button=candidates.find(e=>!/true|on|active|취소|해제|선택됨/.test((e.getAttribute('aria-pressed')||'')+' '+e.className+' '+(e.getAttribute('aria-label')||'')+' '+(e.title||'')));
        if(!button)return candidates.length?'skipped':'failed';
        button.click();await new Promise(r=>setTimeout(r,650));return 'hearted';
      })()`);
      const result = results.find(value => value === "hearted" || value === "skipped") || "failed";
      if (result === "hearted") hearted++; else if (result === "skipped") skipped++; else failed++;
    }
    const summary = { posts: posts.length, hearted, skipped, failed };
    send({ status: "웨일 최근 10일 공감 확인 완료", ...summary, complete: true });
    return summary;
  } finally { page.disconnect(); }
}

async function neighborCommentsInWhale(event, options) {
  const blogId = String(options.blogId || "dicajohn").trim();
  const phrases = unique(options.phrases || []);
  const intervalSeconds = Math.max(15, Math.min(3600, Number(options.intervalSeconds) || 30));
  const requestedMax = Number(options.maxPosts) || 20;
  if (requestedMax > 200) throw new Error("이웃 새글은 한 번에 최대 200개까지 처리할 수 있습니다.");
  if (!phrases.length) throw new Error("이웃 새글 댓글 문구를 한 개 이상 입력해 주세요.");
  const maxPosts = Math.max(1, Math.min(200, requestedMax));
  const send = payload => event.sender.send("neighbor-progress", payload);
  const processedPath = userDataFile("neighbor-comments.json");
  let processed = {};
  try { processed = JSON.parse(fs.readFileSync(processedPath, "utf8")); } catch {}
  neighborJobCancelled = false;
  const page = await openWhalePage("https://section.blog.naver.com/BlogHome.naver");
  try {
    await requireNaverWhaleLogin(page);
    const values = await page.evaluateFrames(`(() => [...document.querySelectorAll('a[href*="blog.naver.com"]')]
      .map(a=>({url:a.href,title:(a.innerText||'').trim()}))
      .filter(x=>/(?:logNo=|blog\\.naver\\.com\\/[\\w.-]+\\/\\d+)/.test(x.url)))()`);
    let links = values.flatMap(value => Array.isArray(value) ? value : []);
    links = links.filter((item, index, all) => all.findIndex(x => x.url === item.url) === index).slice(0, maxPosts);
    let done = 0, skipped = 0, failed = 0;
    for (const item of links) {
      if (neighborJobCancelled) break;
      const key = item.url.replace(/[?#].*$/, "");
      if (processed[key]) { skipped++; continue; }
      send({ status: `웨일 이웃 글 확인: ${item.title || key}`, done, skipped, failed });
      await page.navigate(item.url);
      const results = await page.evaluateFrames(`(async () => {
        const sleep=ms=>new Promise(r=>setTimeout(r,ms));const blogId=${JSON.stringify(blogId)};
        const comments=[...document.querySelectorAll('[class*="comment"],[class*="u_cbox"]')];
        if(comments.some(e=>(e.innerText||'').includes(blogId)))return 'skipped';
        const toggle=[...document.querySelectorAll('button,a')].find(e=>/댓글/.test(e.innerText||''));
        if(toggle){toggle.click();await sleep(450);}
        const editor=document.querySelector('textarea,[contenteditable="true"]');if(!editor)return 'failed';
        const phrases=${JSON.stringify(phrases)},phrase=phrases[Math.floor(Math.random()*phrases.length)];
        editor.focus();
        if(editor.tagName==='TEXTAREA'){
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor,phrase);
          editor.dispatchEvent(new Event('input',{bubbles:true}));
        }else{editor.textContent=phrase;editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:phrase}));}
        await sleep(250);const scope=editor.closest('form,[class*="comment"],[class*="write"]')||document;
        const submit=[...scope.querySelectorAll('button,a')].find(e=>/등록|확인/.test(e.innerText||'')&&!e.disabled);
        if(!submit)return 'failed';submit.click();await sleep(700);return 'done';
      })()`);
      const result = results.find(value => value === "done" || value === "skipped") || "failed";
      if (result === "done") {
        done++; processed[key] = { at: new Date().toISOString() };
        fs.writeFileSync(processedPath, JSON.stringify(processed, null, 2));
      } else if (result === "skipped") skipped++; else failed++;
      send({ status: `${intervalSeconds}초 후 다음 이웃 글로 이동`, done, skipped, failed });
      for (let second = 0; second < intervalSeconds && !neighborJobCancelled; second++) await wait(1000);
    }
    const summary = { found: links.length, done, skipped, failed, stopped: neighborJobCancelled };
    send({ status: neighborJobCancelled ? "사용자가 중지했습니다." : "웨일 이웃 새글 완료", ...summary, complete: true });
    return summary;
  } finally { page.disconnect(); }
}

ipcMain.handle("reply-comments", async (event, options) => {
  if (process.platform === "darwin") return replyCommentsInWhale(event, options);
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

ipcMain.handle("heart-recent-posts", async (event, options) => {
  if (process.platform === "darwin") return heartRecentInWhale(event, options);
  const blogId = String(options.blogId || "dicajohn").trim();
  const win = getNaverWindow();
  const send = payload => event.sender.send("heart-progress", payload);
  win.show();
  await win.loadURL(`https://blog.naver.com/PostList.naver?blogId=${encodeURIComponent(blogId)}&from=postList`);
  await wait(2200);

  let posts = [];
  for (const frame of win.webContents.mainFrame.framesInSubtree) {
    const found = await frame.executeJavaScript(`(() => {
      const cutoff = Date.now() - 10 * 86400000;
      const links = [...document.querySelectorAll('a[href*="logNo="],a[href*="/${blogId}/"]')];
      const out = [];
      for (const a of links) {
        const m = (a.href || '').match(/(?:logNo=|\\/${blogId}\\/)(\\d+)/);
        if (!m || out.some(x => x.logNo === m[1])) continue;
        const box = a.closest('li,article,.post') || a.parentElement;
        const dm = (box?.innerText || '').match(/(20\\d{2})[.\\/-]\\s*(\\d{1,2})[.\\/-]\\s*(\\d{1,2})/);
        const date = dm ? new Date(+dm[1], +dm[2] - 1, +dm[3]).getTime() : Date.now();
        if (date >= cutoff) out.push({logNo:m[1], title:(a.innerText||'').trim(), date});
      }
      return out;
    })()`, true).catch(() => []);
    posts.push(...found);
  }
  posts = posts.filter((p, i, all) => all.findIndex(x => x.logNo === p.logNo) === i);

  let hearted = 0, skipped = 0, failed = 0;
  for (const post of posts) {
    send({ status: `공감 확인: ${post.title || post.logNo}`, hearted, skipped, failed });
    await win.loadURL(`https://blog.naver.com/PostView.naver?blogId=${encodeURIComponent(blogId)}&logNo=${post.logNo}`);
    await wait(1800);
    let result = "failed";
    for (const frame of win.webContents.mainFrame.framesInSubtree) {
      result = await frame.executeJavaScript(`(async () => {
        const candidates = [...document.querySelectorAll('button,a')].filter(e =>
          /공감|좋아요/.test((e.innerText||'') + ' ' + (e.getAttribute('aria-label')||'')));
        const button = candidates.find(e => {
          const state = (e.getAttribute('aria-pressed')||'') + ' ' + e.className + ' ' +
            (e.getAttribute('aria-label')||'') + ' ' + (e.title||'');
          return !/true|on|active|취소|해제|선택됨/.test(state);
        });
        if (!button) return candidates.length ? 'skipped' : 'failed';
        button.click();
        await new Promise(r => setTimeout(r, 650));
        return 'hearted';
      })()`, true).catch(() => "failed");
      if (result !== "failed") break;
    }
    if (result === "hearted") hearted++; else if (result === "skipped") skipped++; else failed++;
    await wait(500);
  }
  const summary = { posts: posts.length, hearted, skipped, failed };
  send({ status: "최근 10일 공감 확인 완료", ...summary, complete: true });
  return summary;
});

ipcMain.handle("stop-neighbor-comments", () => {
  neighborJobCancelled = true;
  return true;
});

ipcMain.handle("comment-neighbor-feed", async (event, options) => {
  if (process.platform === "darwin") return neighborCommentsInWhale(event, options);
  const blogId = String(options.blogId || "dicajohn").trim();
  const phrases = unique(options.phrases || []);
  const intervalSeconds = Math.max(15, Math.min(3600, Number(options.intervalSeconds) || 30));
  const requestedMax = Number(options.maxPosts) || 20;
  if (requestedMax > 200) throw new Error("이웃 새글은 한 번에 최대 200개까지 처리할 수 있습니다.");
  const maxPosts = Math.max(1, Math.min(200, requestedMax));
  if (!phrases.length) throw new Error("이웃 새글 댓글 문구를 한 개 이상 입력해 주세요.");

  neighborJobCancelled = false;
  const win = getNaverWindow();
  const send = payload => event.sender.send("neighbor-progress", payload);
  const processedPath = userDataFile("neighbor-comments.json");
  let processed = {};
  try { processed = JSON.parse(fs.readFileSync(processedPath, "utf8")); } catch {}
  win.show();
  await win.loadURL("https://section.blog.naver.com/BlogHome.naver");
  await wait(2500);

  let links = [];
  for (const frame of win.webContents.mainFrame.framesInSubtree) {
    const found = await frame.executeJavaScript(`(() => [...document.querySelectorAll('a[href*="blog.naver.com"]')]
      .map(a => ({url:a.href, title:(a.innerText||'').trim()}))
      .filter(x => /(?:logNo=|blog\\.naver\\.com\\/[\\w.-]+\\/\\d+)/.test(x.url)))()`, true).catch(() => []);
    links.push(...found);
  }
  links = links.filter((x, i, all) => all.findIndex(y => y.url === x.url) === i).slice(0, maxPosts);

  let done = 0, skipped = 0, failed = 0;
  for (const item of links) {
    if (neighborJobCancelled) break;
    const key = item.url.replace(/[?#].*$/, "");
    if (processed[key]) { skipped++; continue; }
    send({ status: `이웃 글 확인: ${item.title || key}`, done, skipped, failed });
    await win.loadURL(item.url);
    await wait(1900);
    let result = "failed";
    for (const frame of win.webContents.mainFrame.framesInSubtree) {
      result = await frame.executeJavaScript(`(async () => {
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const blogId = ${JSON.stringify(blogId)};
        const pageText = document.body?.innerText || '';
        const comments = [...document.querySelectorAll('[class*="comment"],[class*="u_cbox"]')];
        if (comments.some(e => (e.innerText||'').includes(blogId))) return 'skipped';
        const toggle = [...document.querySelectorAll('button,a')].find(e => /댓글/.test(e.innerText||''));
        if (toggle) { toggle.click(); await sleep(450); }
        const editor = document.querySelector('textarea,[contenteditable="true"]');
        if (!editor) return 'failed';
        const phrases = ${JSON.stringify(phrases)};
        const phrase = phrases[Math.floor(Math.random() * phrases.length)];
        editor.focus();
        if (editor.tagName === 'TEXTAREA') {
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor, phrase);
          editor.dispatchEvent(new Event('input',{bubbles:true}));
        } else {
          editor.textContent = phrase;
          editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:phrase}));
        }
        await sleep(250);
        const scope = editor.closest('form,[class*="comment"],[class*="write"]') || document;
        const submit = [...scope.querySelectorAll('button,a')].find(e => /등록|확인/.test(e.innerText||'') && !e.disabled);
        if (!submit) return 'failed';
        submit.click(); await sleep(700);
        return 'done';
      })()`, true).catch(() => "failed");
      if (result !== "failed") break;
    }
    if (result === "done") {
      done++;
      processed[key] = { at: new Date().toISOString() };
      fs.writeFileSync(processedPath, JSON.stringify(processed, null, 2));
    } else if (result === "skipped") skipped++; else failed++;
    send({ status: `${intervalSeconds}초 후 다음 이웃 글로 이동`, done, skipped, failed });
    for (let second = 0; second < intervalSeconds && !neighborJobCancelled; second++) await wait(1000);
  }
  const summary = { found: links.length, done, skipped, failed, stopped: neighborJobCancelled };
  send({ status: neighborJobCancelled ? "사용자가 작업을 중지했습니다." : "이웃 새글 작업 완료", ...summary, complete: true });
  return summary;
});

app.whenReady().then(() => {
  createMainWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createMainWindow(); });
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });

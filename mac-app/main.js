const { app, BrowserWindow, ipcMain, dialog, shell, session, nativeImage } = require("electron");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const crypto = require("crypto");
const isSmokeTest = process.argv.includes("--smoke-test");

let mainWindow;
let naverWindow;
let neighborJobCancelled = false;
let whaleProcess;
let lastGoogleImageSearch = null;
let lastGoogleCaptureFolder = "";
let activeNaverTask = "";
const WHALE_DEBUG_PORT = 9339;

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const userDataFile = name => path.join(app.getPath("userData"), name);

async function runNaverTask(name, task) {
  if (activeNaverTask) {
    throw new Error(`현재 '${activeNaverTask}' 작업이 진행 중입니다. 완료 또는 중지 후 다시 실행해 주세요.`);
  }
  activeNaverTask = name;
  try {
    return await task();
  } finally {
    activeNaverTask = "";
  }
}

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
  async evaluateMain(expression) {
    const evaluated = await this.send("Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true, userGesture: true
    });
    if (evaluated.exceptionDetails) {
      throw new Error(evaluated.exceptionDetails.text || "웨일 페이지 실행 중 오류가 발생했습니다.");
    }
    return evaluated.result?.value;
  }
  async captureClip(clip) {
    const result = await this.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
      clip: { x: clip.x, y: clip.y, width: clip.width, height: clip.height, scale: 1 }
    });
    return Buffer.from(result.data || "", "base64");
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
    show: !isSmokeTest,
    width: 1180,
    height: 820,
    minWidth: 940,
    minHeight: 680,
    fullscreen: process.platform === "darwin" && !isSmokeTest,
    titleBarStyle: "hiddenInset",
    backgroundColor: "#f4f7fb",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  if (isSmokeTest) {
    mainWindow.webContents.once("did-finish-load", () => app.exit(0));
    mainWindow.webContents.once("did-fail-load", () => app.exit(1));
  }
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

async function collectRealtimeDirect() {
  const definitions = [
    ["다음", "https://adsensefarm.kr/realtime/daum.php", data => data?.data],
    ["구글", "https://adsensefarm.kr/realtime/googletrend.php", data => data?.data],
    ["크리에이터 어드바이저", "https://adsensefarm.kr/realtime/naver.php", data => data?.data],
    ["네이버 시그널", "https://api.signal.bz/news/realtime", data =>
      (data?.top10 || []).map(item => item?.keyword)]
  ];
  const entries = await Promise.all(definitions.map(async ([source, url, pick]) => {
    try {
      const data = JSON.parse(await fetchText(url));
      return [source, unique(pick(data) || []).slice(0, 10)];
    } catch {
      return [source, []];
    }
  }));
  return Object.fromEntries(entries);
}

ipcMain.handle("collect-realtime", async () => {
  const output = await collectRealtimeDirect();
  if (Object.values(output).every(values => values.length >= 10)) return output;
  const collector = new BrowserWindow({
    show: false,
    width: 1400,
    height: 1100,
    webPreferences: { contextIsolation: true, sandbox: true }
  });
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
    for (const [source, values] of Object.entries(adsense || {})) {
      if (!output[source]?.length) output[source] = unique(values).slice(0, 10);
    }

    await collector.loadURL("https://www.signal.bz/");
    await waitForPage(collector, `document.querySelectorAll('.realtime-rank .rank-text,.rank-text').length > 0`);
    const signal = await collector.webContents.executeJavaScript(`(() =>
      [...document.querySelectorAll('.realtime-rank .rank-text,.rank-text')]
        .map(e => (e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim())
        .filter(Boolean).slice(0,10))()`, true);
    if (!output["네이버 시그널"]?.length) output["네이버 시그널"] = unique(signal).slice(0, 10);
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

async function translateToEnglish(keyword) {
  const korean = unique([keyword])[0];
  if (!korean) throw new Error("1번에서 연관 검색어를 먼저 선택해 주세요.");
  const endpoint = "https://translate.googleapis.com/translate_a/single"
    + `?client=gtx&sl=ko&tl=en&dt=t&q=${encodeURIComponent(korean)}`;
  const translatedData = JSON.parse(await fetchText(endpoint));
  const english = (translatedData?.[0] || []).map(part => part?.[0] || "").join("").trim();
  if (!english) throw new Error("영어 번역 결과를 가져오지 못했습니다.");
  return { korean, english };
}

function googleImageUrl(english, creativeCommons = false) {
  const params = new URLSearchParams({
    tbm: "isch", hl: "en", safe: "active", q: english
  });
  if (creativeCommons) params.set("tbs", "il:cl");
  return `https://www.google.com/search?${params.toString()}`;
}

async function openGoogleImages(keyword, creativeCommons = false) {
  const translated = await translateToEnglish(keyword);
  const imageUrl = googleImageUrl(translated.english, creativeCommons);
  if (process.platform === "darwin") {
    const page = await openWhalePage(imageUrl);
    page.disconnect();
  } else {
    await shell.openExternal(imageUrl);
  }
  lastGoogleImageSearch = { ...translated, imageUrl };
  return lastGoogleImageSearch;
}

ipcMain.handle("google-image-search", async (_event, keyword) => {
  return openGoogleImages(keyword, false);
});

ipcMain.handle("open-last-google-images", async () => {
  if (!lastGoogleImageSearch?.imageUrl) throw new Error("먼저 Google 이미지 검색을 실행해 주세요.");
  if (process.platform === "darwin") {
    const page = await openWhalePage(lastGoogleImageSearch.imageUrl);
    page.disconnect();
  } else {
    await shell.openExternal(lastGoogleImageSearch.imageUrl);
  }
  return lastGoogleImageSearch;
});

function googleImageCaptureExpression() {
  return `(async () => {
    const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);
      return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
    const thumbnails=[...document.querySelectorAll('img.YQ4gaf,img.rg_i,div[data-ri] img,a img')]
      .filter(img=>visible(img)&&img.getBoundingClientRect().width>=110&&img.getBoundingClientRect().height>=80);
    let thumbnail=null;
    for(const item of thumbnails){
      if(item.dataset.pictureCleanerTried==='1')continue;
      item.dataset.pictureCleanerTried='1';thumbnail=item;break;
    }
    if(!thumbnail){
      window.scrollBy(0,Math.max(900,window.innerHeight*.85));await sleep(700);
      return {scroll:true};
    }
    thumbnail.scrollIntoView({block:'center'});thumbnail.click();await sleep(850);
    const previews=[...document.querySelectorAll(
      'img.sFlh5c,img.iPVvYb,img.n3VNCb,div[role="dialog"] img,div[aria-live] img')]
      .filter(img=>{const r=img.getBoundingClientRect();return visible(img)&&
        r.left>=window.innerWidth*.45&&r.width>=240&&r.height>=160&&
        (img.naturalWidth||0)>=300&&(img.naturalHeight||0)>=200;})
      .sort((a,b)=>(b.naturalWidth*b.naturalHeight)-(a.naturalWidth*a.naturalHeight));
    if(!previews.length)return {retry:true};
    const preview=previews[0];
    preview.dataset.pictureCleanerOldStyle=preview.getAttribute('style')||'';
    preview.dataset.pictureCleanerCapture='1';
    preview.style.setProperty('object-fit','contain','important');
    preview.style.setProperty('object-position','center center','important');
    await sleep(150);
    const r=preview.getBoundingClientRect();
    const link=preview.closest('a[href]')||thumbnail.closest('a[href]');
    return {
      clip:{x:r.left+window.scrollX,y:r.top+window.scrollY,width:r.width,height:r.height},
      sourceUrl:preview.currentSrc||preview.src||preview.dataset.src||'',
      resultPageUrl:link?.href||''
    };
  })()`;
}

function restoreGoogleImagePreviewExpression() {
  return `(() => {
    const preview=document.querySelector('[data-picture-cleaner-capture="1"]');
    if(!preview)return false;
    preview.setAttribute('style',preview.dataset.pictureCleanerOldStyle||'');
    delete preview.dataset.pictureCleanerOldStyle;
    delete preview.dataset.pictureCleanerCapture;
    return true;
  })()`;
}

async function downloadGooglePreview(sourceUrl) {
  const url = String(sourceUrl || "");
  if (!url || url.startsWith("blob:") || url.startsWith("file:")) return null;
  try {
    let bytes;
    if (url.startsWith("data:image/")) {
      const match = url.match(/^data:image\/[^;,]+;base64,([\s\S]+)$/i);
      if (!match) return null;
      bytes = Buffer.from(match[1], "base64");
    } else {
      const response = await fetch(url, {
        headers: {
          "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X 14_5) AppleWebKit/537.36 Chrome/126 Safari/537.36",
          "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
          "Referer": "https://www.google.com/"
        },
        signal: AbortSignal.timeout(18000)
      });
      if (!response.ok) return null;
      const length = Number(response.headers.get("content-length") || 0);
      if (length > 30 * 1024 * 1024) return null;
      bytes = Buffer.from(await response.arrayBuffer());
    }
    if (!bytes.length || bytes.length > 30 * 1024 * 1024) return null;
    const image = nativeImage.createFromBuffer(bytes);
    const size = image.getSize();
    return !image.isEmpty() && size.width >= 240 && size.height >= 160 ? image : null;
  } catch {
    return null;
  }
}

function saveInternalGoogleImageHistory(records) {
  if (!records.length) return;
  const historyPath = userDataFile("google-image-source-history.json");
  let existing = [];
  try {
    const parsed = JSON.parse(fs.readFileSync(historyPath, "utf8"));
    if (Array.isArray(parsed)) existing = parsed;
  } catch {}
  fs.writeFileSync(historyPath, JSON.stringify([...existing, ...records].slice(-2000), null, 2));
}

function enhanceNativeImage(image, targetLongSide = 2048) {
  let current = image;
  const originalSize = current.getSize();
  const originalLongSide = Math.max(originalSize.width, originalSize.height);
  if (originalLongSide < targetLongSide) {
    const ratio = Math.min(3, targetLongSide / Math.max(1, originalLongSide));
    const finalSize = {
      width: Math.max(1, Math.round(originalSize.width * ratio)),
      height: Math.max(1, Math.round(originalSize.height * ratio))
    };
    if (ratio > 1.65) {
      const middleRatio = Math.sqrt(ratio);
      current = current.resize({
        width: Math.max(1, Math.round(originalSize.width * middleRatio)),
        height: Math.max(1, Math.round(originalSize.height * middleRatio)),
        quality: "best"
      });
    }
    current = current.resize({ ...finalSize, quality: "best" });
  }

  const size = current.getSize();
  const source = current.toBitmap();
  if (!source.length || source.length !== size.width * size.height * 4) return current;
  const output = Buffer.from(source);
  const stride = size.width * 4;
  for (let y = 1; y < size.height - 1; y++) {
    for (let x = 1; x < size.width - 1; x++) {
      const offset = y * stride + x * 4;
      for (let channel = 0; channel < 3; channel++) {
        const value = source[offset + channel];
        const blurred = (
          source[offset - 4 + channel] + source[offset + 4 + channel] +
          source[offset - stride + channel] + source[offset + stride + channel]
        ) / 4;
        const detail = value - blurred;
        const contrasted = 128 + (value - 128) * 1.015;
        output[offset + channel] = Math.max(0, Math.min(255,
          Math.round(contrasted + (Math.abs(detail) >= 4 ? detail * 0.22 : 0))));
      }
    }
  }
  return nativeImage.createFromBitmap(output, {
    width: size.width, height: size.height, scaleFactor: 1
  });
}

async function captureGoogleImages(event, keyword, count = 15) {
  if (process.platform !== "darwin") {
    throw new Error("이미지 자동 저장은 Apple Silicon 맥용 앱에서 실행해 주세요.");
  }
  const targetCount = Math.max(1, Math.min(20, Number(count) || 15));
  const picked = await dialog.showOpenDialog(mainWindow, {
    title: "Google 이미지 저장 폴더 선택",
    properties: ["openDirectory", "createDirectory"]
  });
  if (picked.canceled) return { canceled: true, count: 0 };
  const translated = await translateToEnglish(keyword);
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outputFolder = path.join(picked.filePaths[0], `Google-Images-${timestamp}`);
  fs.mkdirSync(outputFolder, { recursive: true });
  const imageUrl = googleImageUrl(translated.english, true);
  lastGoogleImageSearch = { ...translated, imageUrl };
  const page = await openWhalePage(imageUrl);
  const seenSources = new Set();
  const seenHashes = new Set();
  const metadata = [];
  let saved = 0;
  let originalDownloads = 0;
  let fallbackCaptures = 0;
  try {
    await wait(1600);
    for (let attempt = 0; attempt < 120 && saved < targetCount; attempt++) {
      event.sender.send("google-image-progress", {
        status: `Creative Commons 이미지 확인 중 · ${saved}/${targetCount}`, saved, target: targetCount
      });
      const candidate = await page.evaluateMain(googleImageCaptureExpression()).catch(() => null);
      if (!candidate?.clip) continue;
      const sourceKey = String(candidate.sourceUrl || "");
      let image = null;
      let captureMethod = "original-download";
      try {
        if (sourceKey && seenSources.has(sourceKey)) continue;
        image = await downloadGooglePreview(sourceKey);
        if (!image) {
          captureMethod = "contain-screenshot";
          const png = await page.captureClip(candidate.clip).catch(() => Buffer.alloc(0));
          if (png.length) image = nativeImage.createFromBuffer(png);
        }
      } finally {
        await page.evaluateMain(restoreGoogleImagePreviewExpression()).catch(() => false);
      }
      const size = image?.getSize() || { width: 0, height: 0 };
      if (!image || image.isEmpty() || size.width < 240 || size.height < 160) continue;
      const digest = crypto.createHash("sha256")
        .update(`${size.width}x${size.height}:`).update(image.toBitmap()).digest("hex");
      if (seenHashes.has(digest)) continue;
      const file = `google_cc_${String(saved + 1).padStart(2, "0")}.jpg`;
      fs.writeFileSync(path.join(outputFolder, file), image.toJPEG(94));
      seenHashes.add(digest);
      if (sourceKey) seenSources.add(sourceKey);
      saved++;
      if (captureMethod === "original-download") originalDownloads++; else fallbackCaptures++;
      metadata.push({
        file, query: translated.english, sourceUrl: sourceKey,
        resultPageUrl: candidate.resultPageUrl || "",
        licenseFilter: "Creative Commons", captureMethod, capturedAt: new Date().toISOString()
      });
      event.sender.send("google-image-progress", {
        status: `${captureMethod === "original-download" ? "원본 미리보기" : "전체 보기 캡처"} 저장 · ${saved}/${targetCount}`,
        saved, target: targetCount
      });
    }
  } finally {
    page.disconnect();
  }
  saveInternalGoogleImageHistory(metadata);
  lastGoogleCaptureFolder = outputFolder;
  if (saved < targetCount) {
    throw new Error(`Creative Commons 필터 결과에서 ${targetCount}장 중 ${saved}장만 저장했습니다. 다시 시도해 주세요.`);
  }
  return {
    canceled: false, count: saved, folder: outputFolder,
    originalDownloads, fallbackCaptures, ...translated
  };
}

async function enhanceGoogleImages(event, preferredFolder = "") {
  let sourceFolder = preferredFolder || lastGoogleCaptureFolder;
  if (!sourceFolder || !fs.existsSync(sourceFolder)) {
    const picked = await dialog.showOpenDialog(mainWindow, {
      title: "Google 이미지 15장이 있는 폴더 선택", properties: ["openDirectory"]
    });
    if (picked.canceled) return { canceled: true, count: 0 };
    sourceFolder = picked.filePaths[0];
  }
  const files = fs.readdirSync(sourceFolder)
    .filter(name => /^google_cc_\d+\.jpe?g$/i.test(name)).sort().slice(0, 15);
  if (files.length < 15) throw new Error(`이미지 15장이 필요합니다. 현재 ${files.length}장입니다.`);
  const outputFolder = path.join(sourceFolder, "PictureCleaner");
  fs.mkdirSync(outputFolder, { recursive: true });
  for (let index = 0; index < files.length; index++) {
    event.sender.send("google-image-progress", {
      status: `화질·해상도 개선 중 · ${index + 1}/15`, saved: index, target: 15
    });
    let image = nativeImage.createFromPath(path.join(sourceFolder, files[index]));
    if (image.isEmpty()) throw new Error(`${files[index]} 파일을 읽지 못했습니다.`);
    image = enhanceNativeImage(image, 2048);
    fs.writeFileSync(
      path.join(outputFolder, `cleaned_${String(index + 1).padStart(2, "0")}.jpg`),
      image.toJPEG(96)
    );
  }
  return { canceled: false, count: files.length, folder: outputFolder };
}

ipcMain.handle("capture-google-images", (event, keyword) => captureGoogleImages(event, keyword, 15));
ipcMain.handle("enhance-google-images", event => enhanceGoogleImages(event));
ipcMain.handle("capture-enhance-google-images", async (event, keyword) => {
  const captured = await captureGoogleImages(event, keyword, 15);
  if (captured.canceled) return captured;
  const enhanced = await enhanceGoogleImages(event, captured.folder);
  return { ...captured, enhancedFolder: enhanced.folder };
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

async function recentWhalePosts(page, blogId, days = 10) {
  const safeDays = Math.max(1, Math.min(365, Number(days) || 10));
  try {
    const xml = await fetchText(`https://rss.blog.naver.com/${encodeURIComponent(blogId)}.xml`);
    const nowKorea = new Date(Date.now() + 9 * 3600000);
    const cutoffKorea = Date.UTC(
      nowKorea.getUTCFullYear(), nowKorea.getUTCMonth(),
      nowKorea.getUTCDate() - safeDays + 1, 0, 0, 0
    ) - 9 * 3600000;
    const posts = [...xml.matchAll(/<item\b[\s\S]*?<\/item>/gi)].map(match => {
      const block = match[0];
      const link = (block.match(/<(?:link|guid)[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/(?:link|guid)>/i)?.[1] || "").trim();
      const published = (block.match(/<pubDate[^>]*>([\s\S]*?)<\/pubDate>/i)?.[1] || "").trim();
      const title = (block.match(/<title[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/title>/i)?.[1] || "")
        .replace(/<[^>]+>/g, "").trim();
      const logNo = link.match(/(?:logNo=|\/)(\d{10,})/)?.[1];
      const date = Date.parse(published);
      return logNo && Number.isFinite(date) && date >= cutoffKorea ? { logNo, title, date } : null;
    }).filter(Boolean);
    if (posts.length) {
      return posts.filter((post, index, all) => all.findIndex(x => x.logNo === post.logNo) === index)
        .sort((a, b) => b.date - a.date);
    }
  } catch {}
  await page.navigate(`https://blog.naver.com/PostList.naver?blogId=${encodeURIComponent(blogId)}&from=postList`);
  const values = await page.evaluateFrames(`(() => {
    const cutoff = Date.now() - ${safeDays} * 86400000;
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
  const days = Math.max(1, Math.min(365, Number(options.commentDays) || 10));
  const replyInterval = Math.max(0, Math.min(3600, Number(options.replyInterval) || 0));
  if (!phrases.length) throw new Error("감사 문구를 한 개 이상 입력해 주세요.");
  const send = payload => event.sender.send("reply-progress", payload);
  const processedPath = userDataFile("replied-comments.json");
  let processed = {};
  try { processed = JSON.parse(fs.readFileSync(processedPath, "utf8")); } catch {}
  const page = await openWhalePage("about:blank");
  try {
    await requireNaverWhaleLogin(page);
    const posts = await recentWhalePosts(page, blogId, days);
    let done = 0, liked = 0, skipped = 0, failed = 0;
    for (const post of posts) {
      send({ status: `웨일에서 글 확인: ${post.title || post.logNo}`, done, liked, skipped, failed });
      await page.navigate(`https://blog.naver.com/PostView.naver?blogId=${encodeURIComponent(blogId)}&logNo=${post.logNo}`);
      await Promise.all((await page.evaluateFrames(`(() => {
        const toggle=[...document.querySelectorAll('button,a')].find(e=>(e.innerText||'').includes('댓글'));
        if(toggle)toggle.click(); return true;
      })()`)).map(() => Promise.resolve()));
      await wait(900);
      const results = await page.evaluateFrames(`(async () => {
        const sleep=ms=>new Promise(r=>setTimeout(r,ms));
        const blogId=${JSON.stringify(blogId)}, phrases=${JSON.stringify(phrases)};
        const replyInterval=${JSON.stringify(replyInterval)};
        const processed=${JSON.stringify(processed)}, logNo=${JSON.stringify(post.logNo)};
        for(let page=0;page<30;page++){
          const more=[...document.querySelectorAll('button,a')].find(e=>
            /댓글.*더보기|더보기/.test((e.innerText||'').trim())&&e.offsetParent!==null);
          if(!more)break;more.click();await sleep(350);
        }
        const candidates=[...document.querySelectorAll(
          'ul.u_cbox_list > li.u_cbox_comment,[class*="comment_item"],[class*="u_cbox_comment_box"],li[class*="comment"]')];
        const blocks=candidates.filter((block,index,all)=>!all.some((parent,parentIndex)=>
          parentIndex!==index&&parent.contains(block)&&parent.matches('li.u_cbox_comment')));
        let done=0,liked=0,skipped=0,failed=0;
        for(const block of blocks){
          const author=(block.querySelector('.u_cbox_nick,[class*="name"],[class*="nick"]')?.innerText||'').trim();
          const body=(block.querySelector('.u_cbox_contents,[class*="text"],[class*="contents"]')?.innerText||'').trim();
          const key=logNo+':'+author+':'+body.slice(0,80);
          if(!body||author===blogId){skipped++;continue;}
          const legacyRecord=Boolean(processed[key]);
          const record=processed[key]&&typeof processed[key]==='object'?processed[key]:{};
          if(legacyRecord&&!Object.prototype.hasOwnProperty.call(record,'replied'))record.replied=true;
          const like=[...block.querySelectorAll('.u_cbox_btn_recomm,button,a')].find(e=>
            /공감|좋아요|추천/.test((e.innerText||'')+' '+(e.getAttribute('aria-label')||'')));
          if(like&&!record.liked){
            const state=(like.getAttribute('aria-pressed')||'')+' '+like.className+' '+
              (like.getAttribute('aria-label')||'')+' '+(like.title||'');
            if(/true|_on|active|취소|해제|선택됨/.test(state))record.liked=true;
            else{like.click();await sleep(500);record.liked=true;liked++;}
          }
          processed[key]=record;
          const own=[...block.querySelectorAll('.u_cbox_reply_area li.u_cbox_comment,[class*="reply"] li')]
            .some(e=>(e.querySelector('.u_cbox_nick,[class*="name"],[class*="nick"]')?.innerText||'').includes(blogId));
          if(record.replied||own){record.replied=true;processed[key]=record;skipped++;continue;}
          const reply=[...block.querySelectorAll('.u_cbox_btn_reply,button,a')].find(e=>/답글|답변/.test(e.innerText||''));
          if(!reply){failed++;continue;} reply.click(); await sleep(300);
          const editor=block.querySelector('.u_cbox_write_area textarea.u_cbox_text,textarea,[contenteditable="true"]')||
            document.querySelector('textarea:focus,[contenteditable="true"]:focus');
          if(!editor){failed++;continue;}
          const phrase=phrases[Math.floor(Math.random()*phrases.length)]; editor.focus();
          if(editor.tagName==='TEXTAREA'){
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor,phrase);
            editor.dispatchEvent(new Event('input',{bubbles:true}));
          }else{editor.textContent=phrase;editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:phrase}));}
          const scope=editor.closest('.u_cbox_write_wrap,form,[class*="write"]')||block;
          const submit=[...scope.querySelectorAll('.u_cbox_btn_upload,button,a')].find(e=>/등록|확인/.test(e.innerText||'')&&!e.disabled);
          if(!submit){failed++;continue;} submit.click(); await sleep(650);
          record.replied=true;record.at=new Date().toISOString();record.phrase=phrase;
          processed[key]=record;done++;
          if(replyInterval>0)await sleep(replyInterval*1000);
        }
        return {done,liked,skipped,failed,processed,found:blocks.length};
      })()`);
      const result = results.find(value => value?.found) || { done: 0, liked: 0, skipped: 0, failed: 0, processed };
      done += result.done || 0; liked += result.liked || 0;
      skipped += result.skipped || 0; failed += result.failed || 0;
      processed = result.processed || processed;
      fs.writeFileSync(processedPath, JSON.stringify(processed, null, 2));
    }
    const summary = { posts: posts.length, done, liked, skipped, failed };
    send({ status: "웨일 댓글 답글·하트 완료", ...summary, complete: true });
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
    const candidateMax = Math.min(600, maxPosts * 3);
    let links = await page.evaluateMain(`(async () => {
      const output=[],seen=new Set();let total=${candidateMax},page=1;
      while(output.length<${candidateMax}&&(page-1)*10<total){
        const response=await fetch('/ajax/BuddyPostList.naver?page='+page+'&groupId=0',{credentials:'include'});
        let text=await response.text(),cleaned=text.trimStart();
        if(cleaned.startsWith(")]}',"))cleaned=cleaned.split('\\n').slice(1).join('\\n');
        const data=JSON.parse(cleaned),result=data.result||{},posts=result.buddyPostList||[];
        total=Number(result.buddyPostTotalCount||posts.length||0);
        if(!posts.length)break;
        for(const post of posts){
          let blog=String(post.domainIdOrBlogId||'').trim();
          let logNo=String(post.logNo||'').trim();
          const raw=String(post.postUrl||'').trim();
          if((!blog||!logNo)&&raw){
            const match=raw.match(/blog\\.naver\\.com\\/([^/?#]+)\\/(\\d{10,})/);
            if(match){blog=match[1];logNo=match[2];}
          }
          if(!blog||!logNo||blog.toLowerCase()===${JSON.stringify(blogId.toLowerCase())}||seen.has(logNo))continue;
          seen.add(logNo);output.push({
            url:'https://blog.naver.com/PostView.naver?blogId='+encodeURIComponent(blog)+'&logNo='+logNo,
            logNo,title:String(post.title||post.postTitle||'').trim()
          });
          if(output.length>=${candidateMax})break;
        }
        page++;
      }
      return output;
    })()`).catch(() => []);
    if (!Array.isArray(links) || !links.length) {
      const values = await page.evaluateFrames(`(() => [...document.querySelectorAll('a[href*="blog.naver.com"]')]
        .map(a=>({url:a.href,title:(a.innerText||'').trim()}))
        .filter(x=>/(?:logNo=|blog\\.naver\\.com\\/[\\w.-]+\\/\\d+)/.test(x.url)))()`);
      links = values.flatMap(value => Array.isArray(value) ? value : []);
    }
    links = links.filter((item, index, all) => all.findIndex(x => x.url === item.url) === index)
      .slice(0, candidateMax);
    let done = 0, skipped = 0, failed = 0;
    for (const item of links) {
      if (neighborJobCancelled || done >= maxPosts) break;
      const key = item.logNo || item.url.match(/(?:logNo=|\/)(\d{10,})/)?.[1] || item.url.replace(/[?#].*$/, "");
      if (processed[key] || processed[item.url.replace(/[?#].*$/, "")]) { skipped++; continue; }
      send({ status: `웨일 이웃 글 확인: ${item.title || key}`, done, skipped, failed });
      await page.navigate(item.url);
      const results = await page.evaluateFrames(`(async () => {
        const sleep=ms=>new Promise(r=>setTimeout(r,ms)),blogId=${JSON.stringify(blogId)};
        const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);
          return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
        const toggle=[...document.querySelectorAll('button,a')].find(e=>/댓글/.test(e.innerText||''));
        if(toggle){toggle.click();await sleep(450);}
        const authorLinks=[...document.querySelectorAll(
          '.u_cbox_comment .u_cbox_name a[href],.u_cbox_comment .u_cbox_nick a[href],[class*="comment"] [href*="blog.naver.com"]')];
        if(authorLinks.some(link=>{
          const href=(link.href||'').toLowerCase(),text=(link.innerText||'').trim().toLowerCase();
          return text===blogId.toLowerCase()||href.includes('blogid='+encodeURIComponent(blogId.toLowerCase()))||
            href.includes('blog.naver.com/'+blogId.toLowerCase());
        }))return {state:'skipped',reason:'이미 내 댓글이 있습니다.'};
        const editorSelectors=[
          '.u_cbox_write_area textarea.u_cbox_text','.u_cbox_write_box textarea.u_cbox_text',
          '.u_cbox_write_area [contenteditable="true"].u_cbox_text',
          '.u_cbox_write_box [contenteditable="true"].u_cbox_text',
          '.u_cbox_write_area [contenteditable="true"][role="textbox"]',
          '.u_cbox_write_box [contenteditable="true"][role="textbox"]',
          '.u_cbox_write_area textarea','.u_cbox_write_box textarea'];
        const findEditor=()=>editorSelectors.map(selector=>[...document.querySelectorAll(selector)])
          .flat().find(visible);
        let editor=findEditor();
        if(!editor){
          const launchers=[...document.querySelectorAll(
            '.u_cbox_write_box .u_cbox_guide,.u_cbox_write_area .u_cbox_guide,'+
            '.u_cbox_write_box .u_cbox_inbox,.u_cbox_write_area .u_cbox_inbox,'+
            '.u_cbox_write_box,.u_cbox_write_area')].filter(visible);
          for(const launcher of launchers){
            launcher.scrollIntoView({block:'center'});launcher.click();
            for(let attempt=0;attempt<16&&!editor;attempt++){await sleep(500);editor=findEditor();}
            if(editor)break;
          }
        }
        if(!editor)return {state:'failed',reason:'댓글 입력창을 활성화하지 못했습니다.'};
        const phrases=${JSON.stringify(phrases)},phrase=phrases[Math.floor(Math.random()*phrases.length)];
        editor.scrollIntoView({block:'center'});editor.focus();
        if(editor.tagName==='TEXTAREA'){
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor,phrase);
          editor.dispatchEvent(new Event('input',{bubbles:true}));editor.dispatchEvent(new Event('change',{bubbles:true}));
        }else{
          editor.textContent='';document.execCommand('insertText',false,phrase);
          if(!(editor.textContent||'').trim())editor.textContent=phrase;
          editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:phrase}));
        }
        const uploadSelectors=[
          '.u_cbox_write_area button.u_cbox_btn_upload','.u_cbox_write_area a.u_cbox_btn_upload',
          '.u_cbox_write_box button.u_cbox_btn_upload','.u_cbox_write_box a.u_cbox_btn_upload',
          '.u_cbox_btn_upload'];
        let submit=null;
        for(let attempt=0;attempt<20&&!submit;attempt++){
          submit=uploadSelectors.map(selector=>[...document.querySelectorAll(selector)]).flat()
            .find(element=>visible(element)&&!element.disabled&&element.getAttribute('aria-disabled')!=='true');
          if(!submit)await sleep(500);
        }
        if(!submit)return {state:'failed',reason:'댓글을 입력했지만 등록 버튼이 활성화되지 않았습니다.'};
        submit.click();
        for(let attempt=0;attempt<30;attempt++){
          await sleep(500);
          const value=editor.tagName==='TEXTAREA'?editor.value:(editor.textContent||'');
          if(!editor.isConnected||!visible(editor)||!value.trim())return {state:'done'};
        }
        return {state:'failed',reason:'댓글 등록 완료를 확인하지 못했습니다.'};
      })()`);
      const result = results.find(value => value?.state === "done" || value?.state === "skipped") ||
        results.find(value => value?.state === "failed") || { state: "failed", reason: "댓글 영역을 찾지 못했습니다." };
      if (result.state === "done") {
        done++; processed[key] = { at: new Date().toISOString() };
        fs.writeFileSync(processedPath, JSON.stringify(processed, null, 2));
        send({ status: `댓글 등록 확인 완료 · ${intervalSeconds}초 후 다음 글`, done, skipped, failed });
        for (let second = 0; second < intervalSeconds && !neighborJobCancelled; second++) await wait(1000);
      } else if (result.state === "skipped") {
        skipped++;
        send({ status: result.reason || "이미 댓글을 남긴 글을 건너뜁니다.", done, skipped, failed });
      } else {
        failed++;
        send({ status: result.reason || "댓글 등록에 실패해 다음 글로 이동합니다.", done, skipped, failed });
      }
    }
    const summary = { found: links.length, done, skipped, failed, stopped: neighborJobCancelled };
    send({ status: neighborJobCancelled ? "사용자가 중지했습니다." : "웨일 이웃 새글 완료", ...summary, complete: true });
    return summary;
  } finally { page.disconnect(); }
}

ipcMain.handle("reply-comments", async (event, options) => {
  if (process.platform === "darwin") {
    return runNaverTask("내 글 답글·하트", () => replyCommentsInWhale(event, options));
  }
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
  if (process.platform === "darwin") {
    return runNaverTask("최근 글 하트", () => heartRecentInWhale(event, options));
  }
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
  if (process.platform === "darwin") {
    return runNaverTask("이웃 새글 댓글", () => neighborCommentsInWhale(event, options));
  }
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

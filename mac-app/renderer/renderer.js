const $ = id => document.getElementById(id);
const setBusy = (button, busy) => { button.disabled = busy; };

function imageToJpeg(file) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      const probe = document.createElement("canvas");
      const scale = Math.min(1, 1200 / Math.max(image.width, image.height));
      probe.width = Math.max(1, Math.round(image.width * scale));
      probe.height = Math.max(1, Math.round(image.height * scale));
      const pctx = probe.getContext("2d", { willReadFrequently: true });
      pctx.drawImage(image, 0, 0, probe.width, probe.height);
      const data = pctx.getImageData(0, 0, probe.width, probe.height).data;
      const rowBlank = y => {
        let samples = 0, nearWhite = 0;
        for (let x = 0; x < probe.width; x += Math.max(1, Math.floor(probe.width / 160))) {
          const i = (y * probe.width + x) * 4;
          samples++; if (data[i] > 244 && data[i+1] > 244 && data[i+2] > 244) nearWhite++;
        }
        return nearWhite / samples > .985;
      };
      let top = 0, bottom = probe.height - 1;
      while (top < probe.height * .35 && rowBlank(top)) top++;
      while (bottom > probe.height * .65 && rowBlank(bottom)) bottom--;
      const sy = Math.max(0, Math.floor(top / scale));
      const sh = Math.max(1, Math.ceil((bottom - top + 1) / scale));
      const maxLong = 4096;
      const outScale = Math.min(1, maxLong / Math.max(image.width, sh));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(image.width * outScale);
      canvas.height = Math.round(sh * outScale);
      const ctx = canvas.getContext("2d");
      ctx.filter = "contrast(1.06) saturate(1.03)";
      ctx.drawImage(image, 0, sy, image.width, sh, 0, 0, canvas.width, canvas.height);
      resolve(canvas.toDataURL("image/jpeg", .92));
      URL.revokeObjectURL(image.src);
    };
    image.onerror = reject;
    image.src = URL.createObjectURL(file);
  });
}

$("processImages").onclick = async () => {
  const files = [...$("images").files];
  if (!files.length) return $("imageStatus").textContent = "이미지를 먼저 선택해 주세요.";
  setBusy($("processImages"), true);
  try {
    const output = [];
    for (let i = 0; i < files.length; i++) {
      $("imageStatus").textContent = `${i + 1}/${files.length} 처리 중 · ${files[i].name}`;
      output.push({ name: `${files[i].name.replace(/\.[^.]+$/, "")}-clean.jpg`, dataUrl: await imageToJpeg(files[i]) });
    }
    const result = await window.picture.saveImages(output);
    $("imageStatus").textContent = result.canceled ? "저장을 취소했습니다." : `${result.count}개 완료 · ${result.folder}`;
  } catch (error) { $("imageStatus").textContent = `실패: ${error.message}`; }
  finally { setBusy($("processImages"), false); }
};

let selectedRealtime = "";
let relatedWords = [];
let prefixWords = [];
let selectedImageKeyword = "";

function selectImageKeyword(keyword) {
  selectedImageKeyword = keyword;
  document.querySelectorAll(".related-item").forEach(item =>
    item.classList.toggle("selected", item.dataset.keyword === keyword));
  $("imageSearchKeyword").textContent = keyword;
  $("googleImageSearch").disabled = false;
  $("captureGoogleImages").disabled = false;
  $("captureEnhanceGoogleImages").disabled = false;
  $("goGoogleImages").disabled = false;
  $("googleImageStatus").textContent = "2번 탭에서 영어 번역 후 Google 이미지 검색을 실행할 수 있습니다.";
}

function renderRelatedWords(targetId, words) {
  const target = $(targetId);
  target.innerHTML = words.length
    ? words.map(word => `<button class="related-item" data-keyword="${escapeHtml(word)}">${escapeHtml(word)}</button>`).join("")
    : "";
  target.querySelectorAll(".related-item").forEach(item =>
    item.onclick = () => selectImageKeyword(item.dataset.keyword));
}

function isEphemeral(keyword) {
  const value = String(keyword || "").toLowerCase();
  return [
    /\b\d+\s*[:\-대]\s*\d+\b/, /\b(vs|경기\s*결과|스코어|선발\s*라인업|생중계|중계)\b/i,
    /(축구|야구|농구|배구|골프).*(결과|스코어|중계|라인업)/,
    /(결과|스코어|중계|라인업).*(축구|야구|농구|배구|골프)/,
    /(로또|복권).*(당첨|번호|추첨)/, /(당첨|추첨).*(결과|번호)/,
    /로또\s*\d*회/, /오늘의\s*경기/, /몇\s*대\s*몇/, /득점\s*결과/,
    /(속보|긴급|현재|실시간).*(사고|상황|현황)/,
    /(경기|매치).*(오늘|내일|중계|시간)/
  ].some(pattern => pattern.test(value));
}

function comparisonKey(value) {
  return String(value || "").normalize("NFC").toLocaleLowerCase("ko-KR").replace(/[^\p{L}\p{N}]+/gu, "");
}

function mergedWords(items, excluded = new Set()) {
  const seen = new Set(excluded);
  return items.filter(x => !x.error && x.keyword).map(x => String(x.keyword).normalize("NFC").trim())
    .filter(word => {
      const key = comparisonKey(word);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

async function loadRelated(rawSeed) {
  const seed = String(rawSeed || "").normalize("NFC").replace(/\s+/g, " ").trim();
  if (!seed) {
    $("relatedStatus").textContent = "검색할 키워드를 입력하세요.";
    return;
  }
  selectedRealtime = seed;
  $("useBlogKeyword").disabled = false;
  document.querySelectorAll(".keyword-option").forEach(row =>
    row.classList.toggle("selected", row.dataset.keyword === seed));
  $("manualKeyword").value = seed;
  $("selectedKeyword").textContent = `전체 문구: ${seed}`;
  $("keywordList").innerHTML = '<div class="loading"><span></span>연관 검색어 조회 중</div>';
  $("prefixList").innerHTML = "";
  $("relatedStatus").textContent = "";
  $("prefixStatus").textContent = "";
  $("copyRelated").disabled = true;
  $("copyPrefix").disabled = true;
  $("copyAllRelated").disabled = true;
  try {
    const prefix = seed.split(" ").length > 1 ? seed.split(" ")[0] : "";
    const [items, prefixItems] = await Promise.all([
      window.picture.collectKeywords(seed),
      prefix ? window.picture.collectKeywords(prefix) : Promise.resolve([])
    ]);
    const failed = items.filter(x => x.error).map(x => x.source);
    const excluded = new Set([comparisonKey(seed)]);
    relatedWords = mergedWords(items, excluded);
    const prefixExcluded = new Set([comparisonKey(seed), comparisonKey(prefix)]);
    relatedWords.forEach(word => prefixExcluded.add(comparisonKey(word)));
    prefixWords = prefix ? mergedWords(prefixItems, prefixExcluded) : [];
    renderRelatedWords("keywordList", relatedWords);
    if (!relatedWords.length) $("keywordList").innerHTML = '<div class="selected-empty">표시할 연관 검색어가 없습니다.</div>';
    $("relatedStatus").textContent = failed.length
      ? `${relatedWords.length}개 · ${[...new Set(failed)].join(", ")} 조회 결과 없음`
      : `중복 제거된 연관 검색어 ${relatedWords.length}개`;
    $("copyRelated").disabled = !relatedWords.length;
    $("prefixKeyword").textContent = prefix ? `첫 단어 추가 검색: ${prefix}` : "첫 단어 추가 검색";
    renderRelatedWords("prefixList", prefixWords);
    const prefixFailed = prefixItems.filter(x => x.error).map(x => x.source);
    $("prefixStatus").textContent = prefix
      ? (prefixWords.length
          ? `상단과 중복 제거 · 추가 연관 검색어 ${prefixWords.length}개${prefixFailed.length ? ` · ${[...new Set(prefixFailed)].join(", ")} 조회 실패` : ""}`
          : "상단 결과와 중복되지 않는 추가 검색어가 없습니다.")
      : "여러 단어를 검색하면 첫 단어 결과가 중복 없이 표시됩니다.";
    $("copyPrefix").disabled = !prefixWords.length;
    $("copyAllRelated").disabled = !(relatedWords.length || prefixWords.length);
  } catch (error) {
    relatedWords = [];
    prefixWords = [];
    $("keywordList").innerHTML = "";
    $("relatedStatus").textContent = `조회 실패: ${error.message}`;
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
}

const realtimeSources = ["다음", "구글", "크리에이터 어드바이저", "네이버 시그널"];
const creatorAdvisorCategories = ["세계여행", "비즈니스·경제", "IT·컴퓨터", "교육·학문", "자동차", "게임"];
let realtimeTotals = { crawled: 0, usable: 0, creator: 0 };
let creatorAdvisorLoading = false;
let realtimeLoaded = false;
let realtimeLoading = false;

function keywordSourceGroup(source, input, limit, options = {}) {
  const raw = [...new Set((input || [])
    .map(value => String(value).normalize("NFC").trim()).filter(Boolean))].slice(0, limit);
  const words = options.excludeEphemeral === false ? raw : raw.filter(word => !isEphemeral(word));
  const rows = words.map((word, index) =>
    `<div class="keyword-option" data-keyword="${escapeHtml(word)}"><span class="radio"></span><span>${index + 1}. ${escapeHtml(word)}</span></div>`
  ).join("");
  return {
    rawCount: raw.length,
    usableCount: words.length,
    html: `<div class="source-group${options.creator ? " creator-source" : ""}">
      <div class="source-name"><span>${escapeHtml(source)}</span><span class="source-count">${raw.length}/${limit}</span></div>
      ${rows || `<div class="pane-status">${escapeHtml(options.emptyMessage || "가져온 결과 없음")}</div>`}
    </div>`
  };
}

function wireKeywordOptions(root = $("realtimeSources")) {
  root.querySelectorAll(".keyword-option").forEach(row =>
    row.onclick = () => loadRelated(row.dataset.keyword));
}

function renderRealtimeSummary(extra = "") {
  const creatorSummary = realtimeTotals.creator
    ? ` · 검색 유입 트렌드 ${realtimeTotals.creator}/120개`
    : "";
  $("sourceSummary").textContent =
    `실시간 ${realtimeTotals.crawled}/40개 수집 · 일회성 제외 ${realtimeTotals.usable}개${creatorSummary}${extra}`;
}

async function loadRealtime() {
  if (realtimeLoading) return;
  if (!hydrated) return;
  if (blogRuntime.busy) {
    $("sourceSummary").textContent = "글 작성이 끝난 뒤 새로고침하면 실시간 검색어를 확인할 수 있습니다.";
    return;
  }
  realtimeLoading = true;
  realtimeLoaded = true;
  $("realtimeSources").innerHTML = '<div class="loading"><span></span>4개 출처 연결 중</div>';
  $("sourceSummary").textContent = "실시간 검색어를 자동으로 불러오는 중입니다…";
  setBusy($("refreshRealtime"), true);
  realtimeTotals = { crawled: 0, usable: 0, creator: 0 };
  try {
    const result = await window.picture.collectRealtime();
    const rendered = realtimeSources.map(source => keywordSourceGroup(
      result._sourceModes?.[source] ? `${source} · ${result._sourceModes[source]}` : source,
      result[source],
      10
    ));
    realtimeTotals.crawled = rendered.reduce((sum, group) => sum + group.rawCount, 0);
    realtimeTotals.usable = rendered.reduce((sum, group) => sum + group.usableCount, 0);
    $("realtimeSources").innerHTML = rendered.map(group => group.html).join("");
    wireKeywordOptions();
    renderRealtimeSummary(" · 검색 유입 트렌드 6개 분야 연결 중…");
  } catch (error) {
    $("realtimeSources").innerHTML = "";
    $("sourceSummary").textContent = `실시간 검색어 수집 실패: ${error.message}`;
  }

  const creatorSlot = document.createElement("div");
  creatorSlot.id = "creatorAdvisorSources";
  creatorSlot.innerHTML = `
    <div class="source-divider">
      <b>크리에이터 어드바이저 · 검색 유입 트렌드</b>
      <span>6개 분야 · 각 20개</span>
    </div>
    ${creatorAdvisorCategories.map(category => `
      <div class="source-group creator-source">
        <div class="source-name"><span>${escapeHtml(category)}</span><span class="source-count">연결 중</span></div>
        <div class="loading compact-loading"><span></span>인기 유입 검색어 준비 중</div>
      </div>`).join("")}`;
  $("realtimeSources").appendChild(creatorSlot);

  creatorAdvisorLoading = true;
  try {
    const result = await window.picture.collectCreatorAdvisor($("blogId").value.trim());
    const rendered = creatorAdvisorCategories.map(category =>
      keywordSourceGroup(category, result.groups?.[category], 20, {
        creator: true,
        excludeEphemeral: false,
        emptyMessage: result.errors?.[category] || "가져온 결과 없음"
      }));
    realtimeTotals.creator = rendered.reduce((sum, group) => sum + group.rawCount, 0);
    creatorSlot.innerHTML = `
      <div class="source-divider">
        <b>크리에이터 어드바이저 · 검색 유입 트렌드</b>
        <span>6개 분야 · 각 20개</span>
      </div>
      ${rendered.map(group => group.html).join("")}`;
    wireKeywordOptions(creatorSlot);
    const failedCount = Object.keys(result.errors || {}).length;
    renderRealtimeSummary(failedCount ? ` · ${failedCount}개 분야 조회 실패` : "");
  } catch (error) {
    creatorSlot.innerHTML = `
      <div class="source-divider">
        <b>크리에이터 어드바이저 · 검색 유입 트렌드</b>
        <span>웨일 확인 필요</span>
      </div>
      ${creatorAdvisorCategories.map(category => keywordSourceGroup(category, [], 20, {
        creator: true,
        emptyMessage: "웨일 로그인 후 새로고침해 주세요."
      }).html).join("")}`;
    renderRealtimeSummary(` · 검색 유입 트렌드 조회 실패: ${error.message}`);
  } finally {
    creatorAdvisorLoading = false;
    realtimeLoading = false;
    setBusy($("refreshRealtime"), false);
  }
}

window.picture.onCreatorAdvisorProgress(progress => {
  if (!creatorAdvisorLoading || progress.complete) return;
  renderRealtimeSummary(` · ${progress.index}/${progress.total} ${progress.status}`);
});

$("refreshRealtime").onclick = loadRealtime;
$("manualSearch").onclick = () => loadRelated($("manualKeyword").value);
$("manualKeyword").onkeydown = event => {
  if (event.key === "Enter") {
    event.preventDefault();
    loadRelated($("manualKeyword").value);
  }
};
$("copyRelated").onclick = async () => {
  if (!relatedWords.length) return;
  await navigator.clipboard.writeText(relatedWords.join("\n"));
  $("relatedStatus").textContent = `중복 없는 연관 검색어 ${relatedWords.length}개를 복사했습니다.`;
};
$("copyPrefix").onclick = async () => {
  if (!prefixWords.length) return;
  await navigator.clipboard.writeText(prefixWords.join("\n"));
  $("prefixStatus").textContent = `중복 없는 첫 단어 추가 결과 ${prefixWords.length}개를 복사했습니다.`;
};
$("copyAllRelated").onclick = async () => {
  const all = [...relatedWords, ...prefixWords];
  if (!all.length) return;
  await navigator.clipboard.writeText(all.join("\n"));
  $("relatedStatus").textContent = `중복 없는 상단·하단 결과 ${all.length}개를 복사했습니다.`;
};

const tabTitles = {
  "1": "실시간 연관 검색어",
  "2": "Google 이미지 검색",
  "3": "네이버 블로그",
  "4": "댓글·이웃 소통"
};
function activateTab(tab) {
  document.querySelectorAll(".tab-button").forEach(item => {
    const active = item.dataset.tab === String(tab);
    item.classList.toggle("active", active);
    if (active) {
      document.querySelectorAll(".tab-panel").forEach(panel =>
        panel.classList.toggle("active", panel.dataset.panel === item.dataset.tab));
      document.querySelector("header h1").textContent = tabTitles[item.dataset.tab];
    }
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (String(tab) === "1" && hydrated && !realtimeLoaded && !blogRuntime.smokeTest) loadRealtime();
}
document.querySelectorAll(".tab-button").forEach(button => {
  button.onclick = () => activateTab(button.dataset.tab);
});
$("goGoogleImages").onclick = () => activateTab(2);

$("googleImageSearch").onclick = async () => {
  if (!selectedImageKeyword) {
    $("googleImageStatus").textContent = "1번에서 연관 검색어를 하나 선택해 주세요.";
    return;
  }
  setBusy($("googleImageSearch"), true);
  $("googleImageStatus").textContent = `'${selectedImageKeyword}' 영어 번역 중…`;
  try {
    const result = await window.picture.googleImageSearch(selectedImageKeyword);
    $("googleImageStatus").textContent =
      `${result.korean} → ${result.english} · 웨일에서 Google 이미지 검색 결과를 열었습니다.`;
    $("reopenGoogleImages").disabled = false;
  } catch (error) {
    $("googleImageStatus").textContent = `검색 실패: ${error.message}`;
  } finally {
    setBusy($("googleImageSearch"), false);
  }
};

async function runGoogleImageTask(button, workingText, task) {
  const buttons = ["googleImageSearch", "reopenGoogleImages", "captureGoogleImages",
    "enhanceGoogleImages", "captureEnhanceGoogleImages"].map($);
  buttons.forEach(item => { item.disabled = true; });
  $("googleImageStatus").textContent = workingText;
  try {
    const result = await task();
    if (result?.canceled) {
      $("googleImageStatus").textContent = "폴더 선택을 취소했습니다.";
    } else if (result?.enhancedFolder) {
      $("googleImageStatus").textContent =
        `15장 저장(원본 ${result.originalDownloads || 0} · 전체 보기 캡처 ${result.fallbackCaptures || 0})과 화질·해상도 개선 완료 · ${result.enhancedFolder}`;
    } else if (result?.originalDownloads !== undefined) {
      $("googleImageStatus").textContent =
        `${result.count}장 저장 완료 · 원본 ${result.originalDownloads} · 전체 보기 캡처 ${result.fallbackCaptures} · ${result.folder}`;
    } else if (result?.imageUrl) {
      $("googleImageStatus").textContent =
        `${result.korean} → ${result.english} · 웨일에서 Google 이미지 검색 결과를 열었습니다.`;
    } else {
      $("googleImageStatus").textContent =
        `${result.count || 15}장 완료 · ${result.folder || result.imageUrl || ""}`;
    }
  } catch (error) {
    $("googleImageStatus").textContent = `작업 중단: ${error.message}`;
  } finally {
    $("googleImageSearch").disabled = !selectedImageKeyword;
    $("captureGoogleImages").disabled = !selectedImageKeyword;
    $("captureEnhanceGoogleImages").disabled = !selectedImageKeyword;
    $("enhanceGoogleImages").disabled = false;
    $("reopenGoogleImages").disabled = false;
  }
}

$("reopenGoogleImages").onclick = () =>
  runGoogleImageTask($("reopenGoogleImages"), "최근 Google 이미지 검색 결과를 다시 여는 중…",
    () => window.picture.openLastGoogleImages());
$("captureGoogleImages").onclick = () =>
  runGoogleImageTask($("captureGoogleImages"), "저장 폴더를 선택한 뒤 CC 이미지 15장을 저장합니다…",
    () => window.picture.captureGoogleImages(selectedImageKeyword));
$("enhanceGoogleImages").onclick = () =>
  runGoogleImageTask($("enhanceGoogleImages"), "최근 저장한 이미지 15장의 화질·해상도를 개선합니다…",
    () => window.picture.enhanceGoogleImages());
$("captureEnhanceGoogleImages").onclick = () =>
  runGoogleImageTask($("captureEnhanceGoogleImages"), "CC 이미지 15장 저장 후 화질·해상도를 순서대로 개선합니다…",
    () => window.picture.captureEnhanceGoogleImages(selectedImageKeyword));
window.picture.onGoogleImageProgress(progress => {
  $("googleImageStatus").textContent = progress.status;
  appendProgress(progress.status);
});

function settings() {
  return {
    ...savedSettings,
    blogId: $("blogId").value.trim() || "dicajohn",
    phrases: $("phrases").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
    neighborPhrases: $("neighborPhrases").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
    commentDays: Number($("commentDays").value) || 10,
    replyInterval: Math.max(0, Number($("replyInterval").value) || 0),
    intervalSeconds: Number($("intervalSeconds").value) || 30,
    maxPosts: Number($("maxPosts").value) || 20,
    blog: collectBlogSettings()
  };
}
$("login").onclick = async () => {
  try { await saveSettings(); await window.picture.openNaverLogin($("blogId").value.trim()); }
  catch (error) { showBlogError(error); }
};
$("write").onclick = async () => {
  try { await saveSettings(); await window.picture.openBlogWrite(); }
  catch (error) { showBlogError(error); }
};
$("reply").onclick = async () => {
  setBusy($("reply"), true);
  $("replyStatus").textContent = `최근 ${settings().commentDays}일 글의 댓글·하트를 확인하는 중...`;
  try {
    await saveSettings();
    const result = await window.picture.replyComments(settings());
    $("replyStatus").textContent = `완료 · 글 ${result.posts}개 · 답글 ${result.done}개 · 하트 ${result.liked || 0}개 · 건너뜀 ${result.skipped}개 · 실패 ${result.failed}개`;
  } catch (error) { $("replyStatus").textContent = `중단: ${error.message}`; }
  finally { setBusy($("reply"), false); }
};
$("neighborStart").onclick = async () => {
  setBusy($("neighborStart"), true); $("neighborStatus").textContent = "이웃 새글을 불러오는 중...";
  try {
    await saveSettings();
    const s = settings();
    if (s.maxPosts > 200) throw new Error("이웃 새글은 한 번에 최대 200개까지 입력할 수 있습니다.");
    const result = await window.picture.commentNeighborFeed({
      blogId: s.blogId, phrases: s.neighborPhrases,
      intervalSeconds: s.intervalSeconds, maxPosts: s.maxPosts
    });
    $("neighborStatus").textContent = `${result.stopped ? "중지됨" : "완료"} · 발견 ${result.found}개 · 댓글 ${result.done}개 · 건너뜀 ${result.skipped}개 · 실패 ${result.failed}개`;
  } catch (error) { $("neighborStatus").textContent = `중단: ${error.message}`; }
  finally { setBusy($("neighborStart"), false); }
};
$("neighborStop").onclick = async () => {
  await window.picture.stopNeighborComments();
  $("neighborStatus").textContent = "현재 글 처리 후 중지합니다...";
};
window.picture.onReplyProgress(p => {
  $("replyStatus").textContent = `${p.status} · 답글 ${p.done || 0} · 하트 ${p.liked || 0} · 건너뜀 ${p.skipped || 0} · 실패 ${p.failed || 0}`;
  appendProgress($("replyStatus").textContent);
});
window.picture.onNeighborProgress(p => {
  $("neighborStatus").textContent = `${p.status} · 댓글 ${p.done || 0} · 건너뜀 ${p.skipped || 0} · 실패 ${p.failed || 0}`;
  appendProgress($("neighborStatus").textContent);
});

const PROVIDERS = { chatgpt: "ChatGPT", claude: "Claude", antigravity: "Antigravity" };
const ROLES = ["작성", "교차 검수", "팩트·최신 정보 보강", "문체 다듬기"];
const DEFAULT_STAGES = ["chatgpt", "claude", "antigravity", "chatgpt"].map((provider, index) =>
  ({ provider, role: ROLES[index], model: "" }));
let savedSettings = {};
let blogPreferences = {};
let stageDrafts = DEFAULT_STAGES.map(stage => ({ ...stage }));
let hydrated = false;
let saveTimer = null;
let saveChain = Promise.resolve();
let saveRevision = 0;
let savedRevision = 0;
let savePending = 0;
let closeAfterSave = false;
let blogRuntime = { busy: false, automationEnabled: false };
let blogStarting = false;
let automationChanging = false;
let cliChecking = false;
const cliLoggingIn = new Set();
let progressHeight = 178;
let progressCollapsed = false;
const progressLines = [];
let lastProgressLine = "";

function currentPrompt() {
  return blogPreferences.prompts?.find(prompt => prompt.id === blogPreferences.selectedPromptId);
}

function commitPrompt() {
  const prompt = currentPrompt();
  if (!prompt) return;
  prompt.name = $("blogPromptName").value.trim() || "이름 없는 프롬프트";
  prompt.text = $("blogPromptText").value;
}

function collectBlogSettings() {
  commitPrompt();
  const stageCount = Math.max(1, Math.min(4, Number($("blogStageCount").value) || 1));
  return {
    ...blogPreferences,
    automationEnabled: Boolean(blogRuntime.automationEnabled),
    intervalHours: Number($("blogInterval").value) || 1,
    mode: $("blogMode").value || "draft",
    keyword: $("blogKeyword").value.trim(),
    prompts: blogPreferences.prompts.map(prompt => ({ ...prompt })),
    stages: stageDrafts.slice(0, stageCount).map(stage => ({ ...stage })),
    imageRetryLimit: Number($("blogImageRetries").value),
    includeGoogle: $("blogIncludeGoogle").checked,
    googleReferenceCount: Math.max(1, Math.min(2, Number($("blogGoogleCount").value) || 2)),
    progressHeight,
    progressCollapsed
  };
}

function markSettingsDirty() {
  if (!hydrated) return;
  saveRevision += 1;
  $("settingsStatus").textContent = "변경 내용 저장 중…";
  $("settingsStatus").classList.remove("error");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveSettings().catch(() => {}); }, 500);
}

async function saveSettings() {
  if (!hydrated) throw new Error("설정을 불러온 뒤 다시 실행해 주세요.");
  clearTimeout(saveTimer);
  saveTimer = null;
  const revision = saveRevision;
  // Capture an immutable snapshot now; serialize writes so an older edit cannot win.
  const snapshot = JSON.parse(JSON.stringify(settings()));
  savePending += 1;
  const write = saveChain.catch(() => {}).then(() => window.picture.setSettings(snapshot));
  saveChain = write;
  try {
    await write;
    savedRevision = Math.max(savedRevision, revision);
    savedSettings = snapshot;
    if (revision === saveRevision) {
      $("settingsStatus").textContent = "자동 저장 완료";
      $("settingsStatus").classList.remove("error");
    }
  } catch (error) {
    $("settingsStatus").textContent = "저장 실패 · 다시 수정해 재시도";
    $("settingsStatus").classList.add("error");
    appendProgress(`설정 저장 실패: ${error.message}`);
    throw error;
  } finally { savePending -= 1; }
}

function renderPrompts() {
  $("blogPromptSelect").replaceChildren(...blogPreferences.prompts.map(prompt => {
    const option = document.createElement("option");
    option.value = prompt.id;
    option.textContent = prompt.name;
    return option;
  }));
  $("blogPromptSelect").value = blogPreferences.selectedPromptId;
  const prompt = currentPrompt();
  $("blogPromptName").value = prompt?.name || "";
  $("blogPromptText").value = prompt?.text || "";
  $("blogPromptDelete").disabled = blogPreferences.prompts.length < 2;
}

let cachedModelChoices = {};
function renderStages() {
  const count = Number($("blogStageCount").value) || 1;
  $("blogStages").replaceChildren(...stageDrafts.slice(0, count).map((stage, index) => {
    const row = document.createElement("div");
    row.className = "stage-row";
    row.innerHTML = `<div class="stage-heading"><span>${index + 1}</span><select aria-label="${index + 1}단계 CLI"></select><select aria-label="${index + 1}단계 역할"></select></div><label for="stageModel${index}">모델</label><select id="stageModel${index}" aria-label="${index + 1}단계 모델"></select><input class="custom-model" aria-label="직접 입력할 모델 ID" placeholder="로그인한 CLI의 모델 ID" maxlength="120" hidden>`;
    const [provider, role] = row.querySelectorAll("select");
    for (const [value, label] of Object.entries(PROVIDERS)) provider.add(new Option(label, value));
    for (const value of ROLES) role.add(new Option(value, value));
    if (!ROLES.includes(stage.role)) role.add(new Option(stage.role, stage.role));
    provider.value = stage.provider;
    role.value = stage.role;
    const model = row.querySelector(`#stageModel${index}`);
    // Keep previously saved custom model IDs; CLI defaults remain the safe default.
    for (const value of ["", ...(cachedModelChoices[stage.provider] || (stage.provider === "claude" ? ["sonnet", "opus", "haiku"] : [])), ...stageDrafts.filter(item => item.provider === stage.provider).map(item => item.model || ""), stage.model || ""].filter((v, i, all) => all.indexOf(v) === i)) {
      model.add(new Option(value || "CLI 기본 모델", value));
    }
    model.add(new Option("모델 ID 직접 추가…", "__custom__"));
    model.value = stage.model || "";
    provider.onchange = () => { stageDrafts[index].provider = provider.value; markSettingsDirty(); renderStages(); };
    role.onchange = () => { stageDrafts[index].role = role.value; markSettingsDirty(); };
    const custom = row.querySelector(".custom-model");
    custom.onchange = () => {
      stageDrafts[index].model = custom.value.trim().slice(0, 120);
      markSettingsDirty(); renderStages();
    };
    model.onchange = () => {
      if (model.value === "__custom__") {
        custom.hidden = false; custom.value = stage.model || ""; custom.focus(); return;
      }
      stageDrafts[index].model = model.value;
      markSettingsDirty(); renderStages();
    };
    return row;
  }));
}

function appendProgress(message, time) {
  if (!message) return;
  const value = String(message).slice(0, 32768);
  const stamp = time || new Date().toLocaleTimeString("ko-KR", { hour12: false });
  const line = /^\[\d/.test(value) ? value : `[${stamp}] ${value}`;
  if (line === lastProgressLine) return;
  lastProgressLine = line;
  progressLines.push(...line.split(/\r?\n/));
  if (progressLines.length > 5000) progressLines.splice(0, progressLines.length - 5000);
  $("progressLog").replaceChildren(...progressLines.map(text => {
    const row = document.createElement("span");
    row.textContent = text + "\n";
    if (/실패|오류|failed|error|exception/i.test(text)) row.className = "log-error";
    return row;
  }));
  $("progressLog").scrollTop = $("progressLog").scrollHeight;
}

function showBlogError(error) {
  const message = error?.message || String(error);
  $("blogStatus").textContent = message;
  $("progressSummary").textContent = message;
  $("blogStatusDot").className = "status-dot error";
  appendProgress(message);
}

function updateBlogButtons() {
  const busy = Boolean(blogRuntime.busy || blogStarting);
  $("blogManual").disabled = !hydrated || busy;
  $("blogStop").disabled = !hydrated || !(busy || blogRuntime.automationEnabled);
  $("blogAutomation").disabled = !hydrated || automationChanging;
  $("blogCheckCli").disabled = !hydrated || cliChecking;
  document.querySelectorAll(".cli-login").forEach(button => {
    button.disabled = busy || cliLoggingIn.has(button.dataset.provider);
  });
  for (const id of ["login", "write", "reply", "neighborStart"]) $(id).disabled = busy;
  $("blogStatusDot").classList.toggle("busy", busy);
}

function renderResult(result) {
  if (!result || typeof result !== "object") return;
  $("blogResult").hidden = false;
  const article = result.article || result.draft || {};
  $("blogResultTitle").textContent = result.title || article.title || "작성 결과";
  $("blogResultLocation").textContent = result.runDir || result.run_dir || result.folder || result.path || "";
  let safeUrl = "";
  try {
    const url = new URL(result.url || result.publicationUrl || "");
    if (["http:", "https:"].includes(url.protocol)) safeUrl = url.href;
  } catch {}
  $("blogResultUrl").hidden = !safeUrl;
  $("blogResultUrl").href = safeUrl || "#";
  const paragraphs = result.paragraphs || article.paragraphs || [];
  $("blogResultText").textContent = result.text || article.text || paragraphs.map(paragraph => {
    if (typeof paragraph === "string") return paragraph;
    return [paragraph.subheading || paragraph.heading, paragraph.body || paragraph.content || paragraph.text]
      .filter(Boolean).join("\n\n");
  }).join("\n\n");
  $("blogResultText").parentElement.hidden = !$("blogResultText").textContent;
}

function receiveBlogState(state = {}, { replayLogs = false } = {}) {
  if (!state || typeof state !== "object") return;
  blogRuntime = { ...blogRuntime, ...state };
  if (typeof state.automationEnabled === "boolean") $("blogAutomation").checked = state.automationEnabled;
  $("blogAutomation").nextElementSibling.textContent = blogRuntime.automationEnabled ? "자동화 켜짐" : "자동화 꺼짐";
  const labels = { idle: "수동 작성 대기", running: "글 작성 진행 중", waiting: "다음 자동 실행 대기", stopped: "작업 중지됨", completed: "작성 완료", error: "작업 확인 필요", auth_required: "CLI 로그인 필요" };
  const message = state.message || (state.status ? (labels[state.status] || state.status) : "");
  if (message) {
    $("blogStatus").textContent = message;
    $("progressSummary").textContent = message;
    if (!replayLogs || !state.logs?.length) appendProgress(message, state.time);
  }
  if (replayLogs && Array.isArray(state.logs)) {
    for (const item of state.logs) appendProgress(typeof item === "string" ? item : item.message, item.time);
  }
  const next = blogRuntime.nextRunAt ? new Date(blogRuntime.nextRunAt) : null;
  $("blogNextRun").textContent = next && !Number.isNaN(next.valueOf()) && blogRuntime.automationEnabled
    ? `다음 실행 ${next.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : "";
  $("blogStatusDot").classList.toggle("error", ["error", "auth_required", "failed"].includes(blogRuntime.status));
  if (state.lastResult) renderResult(state.lastResult);
  updateBlogButtons();
}

function renderCliAccounts(response = {}) {
  const values = Array.isArray(response) ? response : (Array.isArray(response.accounts) ? response.accounts : []);
  for (const record of values) {
    if (Array.isArray(record.models)) cachedModelChoices[record.provider] = record.models.filter(value => typeof value === "string" && value.length <= 120);
  }
  renderStages();
  $("blogCliAccounts").replaceChildren(...Object.entries(PROVIDERS).map(([provider, label]) => {
    const record = values.find(item => item.provider === provider || item.name?.toLowerCase() === provider)
      || response.accounts?.[provider] || response[provider] || {};
    const status = typeof record === "string" ? record : record.status;
    const ready = ["ready", "authenticated", "logged_in", "ok", "connected"].includes(status);
    const statuses = { ready: "연결됨", authenticated: "연결됨", logged_in: "연결됨", ok: "연결됨", connected: "연결됨", auth_required: "로그인 필요", not_logged_in: "로그인 필요", missing: "CLI 설치 필요", not_found: "CLI 설치 필요", error: "연결 확인 필요" };
    const node = document.createElement("div");
    node.className = "cli-account";
    const name = document.createElement("strong");
    name.textContent = label;
    const detail = document.createElement("p");
    detail.textContent = [statuses[status] || status || "연결 확인 전", record.message || record.detail].filter(Boolean).join(" · ");
    detail.className = ready ? "account-ready" : "account-error";
    const loginButton = (deviceAuth = false) => {
      const login = document.createElement("button");
      login.type = "button";
      login.className = "secondary cli-login";
      login.dataset.provider = provider;
      login.dataset.deviceAuth = String(deviceAuth);
      login.textContent = deviceAuth ? "기기 코드 로그인" : "로그인";
      if (deviceAuth) login.title = "브라우저 로그인 후 앱으로 돌아오지 못할 때 사용합니다. Terminal에 표시된 안내를 따라 본인 계정으로 인증하세요.";
      login.onclick = async () => {
        if (cliLoggingIn.has(provider)) return;
        cliLoggingIn.add(provider);
        updateBlogButtons();
        try {
          const result = await window.picture.loginCli(provider, { deviceAuth });
          appendProgress(result?.message || `${label} 로그인 창을 열었습니다. 로그인 완료 후 CLI 로그인 재확인을 눌러 주세요.`);
        } catch (error) { showBlogError(error); }
        finally { cliLoggingIn.delete(provider); updateBlogButtons(); }
      };
      return login;
    };
    node.append(name, detail, loginButton());
    if (provider === "chatgpt") node.append(loginButton(true));
    return node;
  }));
  updateBlogButtons();
}

async function checkCliAccounts() {
  if (cliChecking) return;
  cliChecking = true;
  $("blogCheckCli").textContent = "CLI 확인 중…";
  updateBlogButtons();
  try { renderCliAccounts(await window.picture.getCliStatus()); }
  catch (error) { showBlogError(error); }
  finally {
    cliChecking = false;
    $("blogCheckCli").textContent = "CLI 로그인 재확인";
    updateBlogButtons();
  }
}

function applyProgressHeight() {
  const max = Math.max(110, Math.floor(window.innerHeight * 0.7));
  progressHeight = Math.max(110, Math.min(max, progressHeight));
  const height = progressCollapsed ? 52 : progressHeight;
  document.documentElement.style.setProperty("--progress-height", `${height}px`);
  $("progressDock").style.height = `${height}px`;
  $("progressDock").classList.toggle("collapsed", progressCollapsed);
  $("progressToggle").textContent = progressCollapsed ? "펼치기" : "접기";
  $("progressToggle").setAttribute("aria-expanded", String(!progressCollapsed));
  $("progressResize").setAttribute("aria-valuenow", String(Math.round(height)));
}

$("progressToggle").onclick = () => { progressCollapsed = !progressCollapsed; applyProgressHeight(); markSettingsDirty(); };
$("progressResize").onpointerdown = event => {
  if (event.button !== 0) return;
  event.preventDefault();
  const startY = event.clientY;
  const startHeight = $("progressDock").getBoundingClientRect().height;
  progressCollapsed = false;
  const drag = move => { progressHeight = startHeight + startY - move.clientY; applyProgressHeight(); };
  const end = () => {
    window.removeEventListener("pointermove", drag);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    markSettingsDirty();
  };
  window.addEventListener("pointermove", drag);
  window.addEventListener("pointerup", end, { once: true });
  window.addEventListener("pointercancel", end, { once: true });
};
$("progressResize").onkeydown = event => {
  if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  progressCollapsed = false;
  progressHeight += event.key === "ArrowUp" ? 20 : -20;
  applyProgressHeight();
  markSettingsDirty();
};
window.addEventListener("resize", applyProgressHeight);
const progressObserver = new ResizeObserver(() => {
  if (!hydrated || progressCollapsed) return;
  const actual = Math.round($("progressDock").getBoundingClientRect().height);
  if (Math.abs(actual - progressHeight) < 2) return;
  progressHeight = actual;
  document.documentElement.style.setProperty("--progress-height", `${actual}px`);
  markSettingsDirty();
});
progressObserver.observe($("progressDock"));

$("blogPromptSelect").onchange = event => {
  commitPrompt();
  blogPreferences.selectedPromptId = event.target.value;
  renderPrompts();
  markSettingsDirty();
};
$("blogPromptName").oninput = () => {
  commitPrompt();
  const option = $("blogPromptSelect").selectedOptions[0];
  if (option) option.textContent = currentPrompt().name;
  markSettingsDirty();
};
$("blogPromptText").oninput = markSettingsDirty;
$("blogPromptAdd").onclick = () => {
  commitPrompt();
  const source = currentPrompt();
  const prompt = { id: crypto.randomUUID(), name: `${source?.name || "새 프롬프트"} 복사`, text: source?.text || "" };
  blogPreferences.prompts.push(prompt);
  blogPreferences.selectedPromptId = prompt.id;
  renderPrompts();
  $("blogPromptName").focus();
  $("blogPromptName").select();
  markSettingsDirty();
};
$("blogPromptDelete").onclick = () => {
  if (blogPreferences.prompts.length < 2) return;
  blogPreferences.prompts = blogPreferences.prompts.filter(prompt => prompt.id !== blogPreferences.selectedPromptId);
  blogPreferences.selectedPromptId = blogPreferences.prompts[0].id;
  renderPrompts();
  markSettingsDirty();
};
$("blogStageCount").onchange = () => { renderStages(); markSettingsDirty(); };
for (const id of ["blogKeyword", "blogGoogleCount", "blogId", "phrases", "neighborPhrases", "commentDays", "replyInterval", "intervalSeconds", "maxPosts"]) {
  $(id).addEventListener("input", markSettingsDirty);
}
for (const id of ["blogInterval", "blogMode", "blogImageRetries", "blogIncludeGoogle"]) {
  $(id).addEventListener("change", markSettingsDirty);
}
$("useBlogKeyword").onclick = () => {
  $("blogKeyword").value = selectedImageKeyword || selectedRealtime;
  markSettingsDirty();
  activateTab(3);
  $("blogKeyword").focus();
};
$("blogManual").onclick = async () => {
  const keyword = $("blogKeyword").value.trim();
  if (!keyword) { $("blogKeyword").focus(); showBlogError("글을 작성할 키워드를 입력해 주세요."); return; }
  if (blogRuntime.busy || blogStarting) return;
  blogStarting = true;
  updateBlogButtons();
  try {
    await saveSettings();
    receiveBlogState(await window.picture.startManualBlog({ keyword, mode: $("blogMode").value }));
  } catch (error) { showBlogError(error); }
  finally { blogStarting = false; updateBlogButtons(); }
};
$("blogKeyword").onkeydown = event => {
  if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); $("blogManual").click(); }
};
$("blogAutomation").onchange = async event => {
  const desired = event.target.checked;
  automationChanging = true;
  updateBlogButtons();
  try {
    await saveSettings();
    receiveBlogState(await window.picture.setBlogAutomation(desired));
  } catch (error) {
    $("blogAutomation").checked = blogRuntime.automationEnabled;
    showBlogError(error);
  } finally { automationChanging = false; updateBlogButtons(); }
};
$("blogStop").onclick = async () => {
  try { receiveBlogState(await window.picture.stopBlog()); }
  catch (error) { showBlogError(error); }
};
$("blogCheckCli").onclick = checkCliAccounts;
$("blogOpenFolder").onclick = async () => {
  try { await window.picture.openBlogFolder(); }
  catch (error) { showBlogError(error); }
};
const unsubscribeBlogProgress = window.picture.onBlogProgress(state => receiveBlogState(state));

window.addEventListener("beforeunload", event => {
  if (!hydrated || closeAfterSave) { unsubscribeBlogProgress(); return; }
  if (saveTimer || savePending || savedRevision < saveRevision) {
    event.preventDefault();
    event.returnValue = false;
    saveSettings().then(() => { closeAfterSave = true; window.close(); }).catch(() => {});
  } else unsubscribeBlogProgress();
});

(async () => {
  try {
    const saved = await window.picture.getSettings();
    savedSettings = saved;
    if (saved.blogId) $("blogId").value = saved.blogId;
    if (Array.isArray(saved.phrases)) $("phrases").value = saved.phrases.join("\n");
    if (Array.isArray(saved.neighborPhrases)) $("neighborPhrases").value = saved.neighborPhrases.join("\n");
    for (const key of ["commentDays", "replyInterval", "intervalSeconds", "maxPosts"]) {
      if (saved[key] !== undefined) $(key).value = saved[key];
    }
    const preferences = saved.blog || {};
    blogPreferences = {
      ...preferences,
      prompts: preferences.prompts?.length ? preferences.prompts.map(prompt => ({ ...prompt })) : [{ id: "default", name: "기본 글쓰기", text: "" }],
      selectedPromptId: preferences.selectedPromptId || "default"
    };
    if (!currentPrompt()) blogPreferences.selectedPromptId = blogPreferences.prompts[0].id;
    const savedStages = preferences.stages?.length ? preferences.stages.slice(0, 4) : DEFAULT_STAGES;
    stageDrafts = DEFAULT_STAGES.map((stage, index) => ({ ...stage, ...savedStages[index] }));
    $("blogStageCount").value = String(savedStages.length);
    $("blogInterval").value = String(preferences.intervalHours || 1);
    $("blogMode").value = preferences.mode || "draft";
    $("blogKeyword").value = preferences.keyword || "";
    $("blogIncludeGoogle").checked = preferences.includeGoogle !== false;
    $("blogImageRetries").value = String(preferences.imageRetryLimit ?? 2);
    $("blogGoogleCount").value = preferences.googleReferenceCount ?? 2;
    progressHeight = Number(preferences.progressHeight) || 178;
    progressCollapsed = Boolean(preferences.progressCollapsed);
    blogRuntime.automationEnabled = Boolean(preferences.automationEnabled);
    renderPrompts();
    renderStages();
    applyProgressHeight();
    hydrated = true;
    for (const id of ["blogInterval", "blogMode", "blogKeyword", "blogPromptSelect", "blogPromptName", "blogPromptText", "blogPromptAdd", "blogStageCount", "blogIncludeGoogle", "blogImageRetries", "blogGoogleCount"]) $(id).disabled = false;
    $("settingsStatus").textContent = "이전 설정 불러옴";
    receiveBlogState(await window.picture.getBlogState(), { replayLogs: true });
    renderCliAccounts();
    if (!blogRuntime.smokeTest) {
      // Backend owns automation startup. Merely opening the UI never starts a run.
      if (!blogRuntime.busy) checkCliAccounts();
      $("sourceSummary").textContent = "1번 탭을 열면 실시간 검색어를 가져옵니다.";
    }
  } catch (error) {
    showBlogError(`프로그램 초기화 실패: ${error.message}`);
    $("settingsStatus").textContent = "초기화 확인 필요";
    $("settingsStatus").classList.add("error");
  }
})();

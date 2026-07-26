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

function isEphemeral(keyword) {
  const value = String(keyword || "").toLowerCase();
  return [
    /\b\d+\s*[:\-대]\s*\d+\b/, /\b(vs|경기\s*결과|스코어|선발\s*라인업|생중계|중계)\b/i,
    /(축구|야구|농구|배구|골프).*(결과|스코어|중계|라인업)/,
    /(결과|스코어|중계|라인업).*(축구|야구|농구|배구|골프)/,
    /(로또|복권).*(당첨|번호|추첨)/
  ].some(pattern => pattern.test(value));
}

function comparisonKey(value) {
  return String(value || "").normalize("NFC").toLocaleLowerCase("ko-KR").replace(/[\W_]+/gu, "");
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
    $("keywordList").innerHTML = relatedWords.length
      ? relatedWords.map(word => `<div class="related-item">${escapeHtml(word)}</div>`).join("")
      : '<div class="selected-empty">표시할 연관 검색어가 없습니다.</div>';
    $("relatedStatus").textContent = failed.length
      ? `${relatedWords.length}개 · ${[...new Set(failed)].join(", ")} 조회 결과 없음`
      : `중복 제거된 연관 검색어 ${relatedWords.length}개`;
    $("copyRelated").disabled = !relatedWords.length;
    $("prefixKeyword").textContent = prefix ? `첫 단어 추가 검색: ${prefix}` : "첫 단어 추가 검색";
    $("prefixList").innerHTML = prefixWords.length
      ? prefixWords.map(word => `<div class="related-item">${escapeHtml(word)}</div>`).join("")
      : "";
    const prefixFailed = prefixItems.filter(x => x.error).map(x => x.source);
    $("prefixStatus").textContent = prefix
      ? (prefixWords.length
          ? `상단과 중복 제거 · 추가 연관 검색어 ${prefixWords.length}개${prefixFailed.length ? ` · ${[...new Set(prefixFailed)].join(", ")} 조회 실패` : ""}`
          : "상단 결과와 중복되지 않는 추가 검색어가 없습니다.")
      : "여러 단어를 검색하면 첫 단어 결과가 중복 없이 표시됩니다.";
    $("copyPrefix").disabled = !prefixWords.length;
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

async function loadRealtime() {
  $("realtimeSources").innerHTML = '<div class="loading"><span></span>4개 출처 연결 중</div>';
  $("sourceSummary").textContent = "실시간 검색어를 자동으로 불러오는 중입니다…";
  setBusy($("refreshRealtime"), true);
  try {
    const result = await window.picture.collectRealtime();
    const sources = ["다음", "구글", "크리에이터 어드바이저", "네이버 시그널"];
    let crawled = 0, usable = 0;
    $("realtimeSources").innerHTML = sources.map(source => {
      const raw = [...new Set((result[source] || []).map(x => String(x).trim()).filter(Boolean))];
      const words = raw.filter(word => !isEphemeral(word));
      crawled += raw.length; usable += words.length;
      const rows = words.map((word, index) =>
        `<div class="keyword-option" data-keyword="${escapeHtml(word)}"><span class="radio"></span><span>${index + 1}. ${escapeHtml(word)}</span></div>`
      ).join("");
      return `<div class="source-group"><div class="source-name">${source}<span class="source-count">${raw.length}/10</span></div>${rows || '<div class="pane-status">가져온 결과 없음</div>'}</div>`;
    }).join("");
    $("realtimeSources").querySelectorAll(".keyword-option").forEach(row =>
      row.onclick = () => loadRelated(row.dataset.keyword));
    $("sourceSummary").textContent = `4개 출처 총 ${crawled}/40개 수집 · 일회성 제외 ${usable}개`;
  } catch (error) {
    $("realtimeSources").innerHTML = "";
    $("sourceSummary").textContent = `실시간 검색어 수집 실패: ${error.message}`;
  } finally { setBusy($("refreshRealtime"), false); }
}

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

function settings() {
  return {
    blogId: $("blogId").value.trim() || "dicajohn",
    phrases: $("phrases").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
    neighborPhrases: $("neighborPhrases").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean),
    intervalSeconds: Number($("intervalSeconds").value) || 30,
    maxPosts: Number($("maxPosts").value) || 20
  };
}
async function saveSettings() { await window.picture.setSettings(settings()); }
$("login").onclick = async () => { await saveSettings(); await window.picture.openNaverLogin($("blogId").value.trim()); };
$("write").onclick = () => window.picture.openBlogWrite();
$("reply").onclick = async () => {
  setBusy($("reply"), true); $("replyStatus").textContent = "최근 10일 글을 확인하는 중...";
  try {
    await saveSettings();
    const result = await window.picture.replyComments(settings());
    $("replyStatus").textContent = `완료 · 글 ${result.posts}개 · 답글 ${result.done}개 · 건너뜀 ${result.skipped}개 · 실패 ${result.failed}개`;
  } catch (error) { $("replyStatus").textContent = `중단: ${error.message}`; }
  finally { setBusy($("reply"), false); }
};
$("heart").onclick = async () => {
  setBusy($("heart"), true); $("heartStatus").textContent = "최근 10일 글의 공감 상태 확인 중...";
  try {
    await saveSettings();
    const result = await window.picture.heartRecentPosts(settings());
    $("heartStatus").textContent = `완료 · 글 ${result.posts}개 · 하트 ${result.hearted}개 · 건너뜀 ${result.skipped}개 · 실패 ${result.failed}개`;
  } catch (error) { $("heartStatus").textContent = `중단: ${error.message}`; }
  finally { setBusy($("heart"), false); }
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
  $("replyStatus").textContent = `${p.status} · 답글 ${p.done || 0} · 건너뜀 ${p.skipped || 0} · 실패 ${p.failed || 0}`;
});
window.picture.onHeartProgress(p => {
  $("heartStatus").textContent = `${p.status} · 하트 ${p.hearted || 0} · 건너뜀 ${p.skipped || 0} · 실패 ${p.failed || 0}`;
});
window.picture.onNeighborProgress(p => {
  $("neighborStatus").textContent = `${p.status} · 댓글 ${p.done || 0} · 건너뜀 ${p.skipped || 0} · 실패 ${p.failed || 0}`;
});
(async () => {
  loadRealtime();
  const saved = await window.picture.getSettings();
  if (saved.blogId) $("blogId").value = saved.blogId;
  if (saved.phrases?.length) $("phrases").value = saved.phrases.join("\n");
  if (saved.neighborPhrases?.length) $("neighborPhrases").value = saved.neighborPhrases.join("\n");
  if (saved.intervalSeconds) $("intervalSeconds").value = saved.intervalSeconds;
  if (saved.maxPosts) $("maxPosts").value = saved.maxPosts;
})();

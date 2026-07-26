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

$("keywords").onclick = async () => {
  const seed = $("seed").value.trim();
  if (!seed) return $("keywordList").textContent = "기준 검색어를 입력해 주세요.";
  setBusy($("keywords"), true); $("keywordList").textContent = "수집 중...";
  try {
    const items = await window.picture.collectKeywords(seed);
    $("keywordList").innerHTML = items.map(x => `<div class="keyword"><b>${x.source}</b>${x.keyword}</div>`).join("");
  } catch (error) { $("keywordList").textContent = `실패: ${error.message}`; }
  finally { setBusy($("keywords"), false); }
};

function settings() {
  return { blogId: $("blogId").value.trim(), phrases: $("phrases").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean) };
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
window.picture.onReplyProgress(p => {
  $("replyStatus").textContent = `${p.status} · 답글 ${p.done || 0} · 건너뜀 ${p.skipped || 0} · 실패 ${p.failed || 0}`;
});
(async () => {
  const saved = await window.picture.getSettings();
  if (saved.blogId) $("blogId").value = saved.blogId;
  if (saved.phrases?.length) $("phrases").value = saved.phrases.join("\n");
})();

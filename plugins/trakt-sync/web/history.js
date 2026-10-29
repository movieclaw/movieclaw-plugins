"use strict";
const $ = id => document.getElementById(id);
const labels = {scanning:"扫描阶段", ready:"等待核对确认", running:"写入阶段", paused:"已暂停", complete:"全部完成", complete_with_errors:"有条目未同步", cancelled:"已取消"};
const active = t => t && ["scanning", "running"].includes(t.state);
let member = Number(new URL(location.href).searchParams.get("member_id") || 0);
if (!Number.isSafeInteger(member) || member < 0) member = 0;
$("member").value = member;
let task, driving = false, halted = false, serial = 0, timer, offset = 0;
let detailId, detailPage = 0, detailRecords = false;
function el(tag, text) { const n = document.createElement(tag); if (text != null) n.textContent = String(text); return n; }
function date(t) { return new Date(t).toLocaleString(); }
function counts(t) { return `${t.movies || 0} 部电影，${t.shows || 0} 部剧集 / ${t.seasons || 0} 季 / ${t.episodes || 0} 集，共 ${t.total} 条观看记录（含重看）`; }
function precision(t) {
  if (t.direction !== "export") return "";
  return `Trakt 按 UTC 分钟对齐，MovieClaw 原始时间保留。${t.minute_collisions ? `检测到 ${t.minute_collisions} 条同一分钟的额外观看，无法在 Trakt 分别保存；结果会列为跳过。` : "同一分钟的远端匹配只代表分钟级对齐。"}`;
}
function description(t) {
  if (t.state === "complete") return `已将 ${date(t.cutoff)} 之前本次匹配且未同步的 ${t.done} 条记录${t.direction === "export" ? "按 Trakt 分钟精度对齐" : "全部同步"}。库外或缺少匹配编号的条目未同步。`;
  return `${labels[t.state] || t.state} · 已完成 ${t.done} · 失败 ${t.failed} · 跳过 ${t.skipped} · 未匹配 ${t.unmatched}`;
}
async function api(path, method = "GET", body) {
  const url = new URL(`/api/v1/plugins/trakt-sync/history${path}`, location.origin);
  url.searchParams.set("member_id", member);
  const r = await fetch(url, {method, credentials:"same-origin", cache:"no-store", headers:{"Content-Type":"application/json"}, body:body === undefined ? undefined : JSON.stringify(body)});
  const v = await r.json();
  if (!r.ok || v.success === false) throw new Error(r.status === 403 ? "需要管理员身份及对应操作授权" : v.message || v.detail || "请求未完成");
  return v.data ?? v;
}
function error(e) { $("error").textContent = e ? e.message || String(e) : ""; $("error").hidden = !e; }
function action(fn) { return async () => { error(null); try { await fn(); } catch(e) { error(e); } }; }
function render() {
  $("current-section").hidden = !task;
  $("export").disabled = $("import").disabled = !!(task && !["complete", "complete_with_errors", "cancelled"].includes(task.state));
  if (!task) return;
  const phase = active(task) ? (driving ? (task.state === "scanning" ? "正在扫描" : "正在写入") : "等待在此页继续") : labels[task.state];
  $("current-title").textContent = `${task.direction === "export" ? "MovieClaw → Trakt" : "Trakt → MovieClaw"} · ${phase}`;
  $("progress-text").textContent = task.state === "scanning" ? `已检查 ${task.scanned} 个单元 / 历史记录；待同步 ${task.total} 条，未匹配 ${task.unmatched}` : `${counts(task)}。${description(task)}`;
  $("task-error").textContent = task.error || "";
  $("precision-notice").textContent = precision(task);
  $("precision-notice").hidden = !precision(task);
  $("keep-open").hidden = ["complete", "complete_with_errors", "cancelled", "ready"].includes(task.state);
  const p = $("progress");
  if (task.state === "scanning") p.removeAttribute("value");
  else p.value = task.total ? Math.min(100, 100 * (task.processed ?? (task.done + task.failed + task.skipped)) / task.total) : 100;
  $("confirm").hidden = task.state !== "ready";
  $("preview").hidden = !task.group_pages;
  $("preview").textContent = task.state === "ready" ? "查看待同步条目" : "查看同步详情";
  $("continue").hidden = !(task.state === "paused" || task.state === "complete_with_errors" || (active(task) && !driving));
  $("pause").hidden = !driving;
  $("cancel").hidden = ["complete", "cancelled"].includes(task.state);
}
async function history(append = false) {
  const own = serial;
  const data = await api(`?offset=${offset}`);
  if (own !== serial) return;
  const root = $("history"); if (!append) root.replaceChildren();
  for (const t of data.tasks) {
    const row = el("article"); row.className = "card preferences";
    row.append(el("strong", `${t.direction === "export" ? "MovieClaw → Trakt" : "Trakt → MovieClaw"} · ${date(t.created_at)}`), el("p", description(t)));
    const b = el("button", "查看详情"); b.onclick = action(() => details(t.id)); row.append(b);
    if (["scanning", "running", "paused", "ready", "complete_with_errors"].includes(t.state)) {
      const select = el("button", "打开此任务"); select.onclick = action(async () => { stop(); serial++; task = (await api(`/${t.id}`)).task; render(); }); row.append(select);
    }
    root.append(row);
  }
  if (!data.total) root.append(el("p", "暂无同步历史。扫描与写入都需要你手动触发。"));
  $("more-history").hidden = offset + data.tasks.length >= data.total;
}
async function details(id, page = 0, records = false) {
  const data = await api(`/${encodeURIComponent(id)}?page=${page}&records=${records}`);
  detailId = id; detailPage = page; detailRecords = records;
  $("detail-summary").textContent = `${counts(data.task)}。${description(data.task)} ${precision(data.task)}`;
  const root = $("detail-items"); root.replaceChildren();
  for (const v of data.items) {
    const row = el("article"); row.className = "card preferences";
    if (records) {
      row.append(el("strong", v.media.title + (v.media.kind === "tv" ? ` · S${v.season}E${v.episode}` : "")), el("p", `${date(v.watched_at)} · ${{pending:"待处理", sent:"已同步", skipped:"已跳过", failed:"失败"}[v.state] || v.state}${v.reason ? " · " + v.reason : ""}`));
      if (v.trakt_watched_at) row.append(el("p", `Trakt 时间：${date(v.trakt_watched_at)}${v.minute_collision ? " · 同分钟额外观看，无法分别保存" : ""}（MovieClaw 原始时间不变）`));
      if (v.basis === "state") row.append(el("p", "时间来自本地已看状态，无法据此补出更早的重看记录。"));
    } else {
      row.append(el("strong", v.title + (v.kind === "tv" ? ` · 第 ${v.season} 季` : "")), el("p", `${v.kind === "tv" ? v.episodes.length + " 集 · " : ""}${v.records} 条观看记录${v.state_time_records ? ` · ${v.state_time_records} 条采用本地状态时间` : ""}`));
    }
    root.append(row);
  }
  if (!data.items.length) root.append(el("p", "暂无条目"));
  $("detail-page").textContent = `${data.pages ? page + 1 : 0} / ${data.pages}`;
  $("previous").disabled = page === 0; $("next").disabled = page + 1 >= data.pages;
  if (!$("detail").open) $("detail").showModal();
}
function stop() { halted = true; clearTimeout(timer); driving = false; render(); }
async function drive() {
  if (driving || !active(task) || document.hidden) return;
  halted = false; driving = true; const own = serial; render();
  async function tick() {
    if (halted || own !== serial || document.hidden || !active(task)) { driving = false; render(); return; }
    let updated;
    try { updated = await api(`/${encodeURIComponent(task.id)}/tick`, "POST"); }
    catch(e) { error(e); stop(); return; }
    if (own !== serial || halted) return;
    task = updated;
    if (!active(task)) { driving = false; render(); offset = 0; await history(); return; }
    render();
    const delay = task.next_at ? Math.max(500, task.next_at * 1000 - Date.now()) : 150;
    timer = setTimeout(tick, Math.min(delay, 3600000));
  }
  await tick();
}
for (const direction of ["export", "import"]) $(direction).onclick = action(async () => { task = await api("", "POST", {direction}); await history(); await drive(); });
$("confirm").onclick = action(async () => { task = await api(`/${task.id}/confirm`, "POST", {token:task.preview_token}); await drive(); });
$("preview").onclick = action(() => details(task.id));
$("continue").onclick = action(async () => { if (["paused", "complete_with_errors"].includes(task.state)) task = await api(`/${task.id}/resume`, "POST"); await drive(); });
$("pause").onclick = action(async () => { stop(); task = await api(`/${task.id}/pause`, "POST"); render(); });
$("cancel").onclick = action(async () => { stop(); task = await api(`/${task.id}/cancel`, "POST"); render(); offset = 0; await history(); });
$("member").onchange = action(async () => { const id = Number($("member").value); if (!Number.isSafeInteger(id) || id < 0 || !$("member").value) throw new Error("成员 ID 无效"); stop(); serial++; member = id; task = null; offset = 0; render(); await history(); });
$("groups").onclick = action(() => details(detailId)); $("records").onclick = action(() => details(detailId, 0, true));
$("previous").onclick = action(() => details(detailId, detailPage - 1, detailRecords)); $("next").onclick = action(() => details(detailId, detailPage + 1, detailRecords));
$("more-history").onclick = action(async () => { offset += 20; await history(true); });
window.addEventListener("beforeunload", e => { if (driving) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("visibilitychange", () => { if (document.hidden && driving) { stop(); $("notice").textContent = "已停止发起下一批。回到此页后点击继续。"; $("notice").hidden = false; } });
action(() => history())();

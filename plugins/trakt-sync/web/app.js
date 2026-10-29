"use strict";
const $ = (id) => document.getElementById(id);
const states = {queued:"待发送", retrying:"自动重试中", blocked:"需要重新连接", failed:"发送失败", sent:"已发送", skipped:"未发送"};
const events = {started:"开始播放", progress:"更新播放进度", stopped:"停止播放", completed:"观看完成", marked_played:"手动标记已看", marked_unplayed:"手动取消已看", favorited:"收藏加入 Watchlist"};
let generation = 0, revision = 0, selected = 0, filter = "all", records = [], busy = false;
let accountSnapshot = "", recordSnapshot = "", noticeTimer, activityType = "important";
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function memberLabel(id) { return id === 0 ? "管理员" : `成员 ${id}`; }
function readMember() {
  const n = Number($("member").value);
  if (!Number.isSafeInteger(n) || n < 0 || $("member").value.trim() === "") throw new Error("请输入有效的成员 ID");
  return n;
}
function time(value) {
  if (!value) return "暂无发送";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString(undefined, {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"});
}
let savingProfile = false;
async function api(path, method = "GET", id = selected, body) {
  const url = new URL(`/api/v1/plugins/trakt-sync/${path}`, location.origin);
  url.searchParams.set("member_id", id);
  const response = await fetch(url, {method, credentials:"same-origin", headers:{Accept:"application/json", "Content-Type":"application/json"}, cache:"no-store", body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok || result.success === false) throw new Error(response.status === 403 ? "你没有权限操作这个成员的资料" : result.message || result.detail || "请求未完成");
  return result.data ?? result;
}
function showError(message) { $("error").textContent = message || ""; $("error").hidden = !message; }
function notice(message) {
  clearTimeout(noticeTimer); $("notice").textContent = message; $("notice").hidden = false;
  noticeTimer = setTimeout(() => { $("notice").hidden = true; },8000);
}
function action(fn) {
  return async () => { showError(""); try { await fn(); } catch (error) { showError(error.message || "操作暂未完成，请重试"); } };
}
function cancelAuth() { generation++; busy = false; if ($("authorization").open) $("authorization").close(); }
async function selectMember(id) {
  if (id !== selected) { cancelAuth(); selected = id; }
  $("member").value = String(id);
  await refresh();
}
function renderAccounts(accounts) {
  const container = $("accounts"); container.replaceChildren();
  for (const account of accounts) {
    const row = el("article", `account-row${account.member_id === selected ? " selected" : ""}`);
    const button = el("button", "account-select");
    button.setAttribute("aria-pressed", String(account.member_id === selected));
    button.setAttribute("aria-label", `查看${memberLabel(account.member_id)}的同步记录`);
    button.onclick = action(() => selectMember(account.member_id));
    button.append(el("span", "avatar", (account.username || memberLabel(account.member_id)).slice(0,1).toUpperCase()));
    const identity = el("div", "account-identity");
    identity.append(el("div", "account-name", account.username || memberLabel(account.member_id)), el("p", "account-meta", account.username ? memberLabel(account.member_id) : "连接自己的 Trakt 账号"));
    const status = el("div", "account-state");
    status.append(el("span", `badge ${account.last_error ? "blocked" : account.connected ? "ok" : account.has_account ? "blocked" : ""}`, account.needs_reconnect ? "需要重新连接" : account.last_error ? "同步需检查" : account.connected ? "● 已连接" : account.has_account ? "需要重新连接" : "尚未连接"));
    const counts = el("p");
    counts.append(el("span", "", `${account.pending} 待发送 · `), el("span", account.failed ? "has-error" : "", `${account.failed} 条保留的失败记录`));
    status.append(counts, el("p", "", [account.last_error, account.warning].filter(Boolean).join("；") || (account.last_success ? `上次发送 ${time(account.last_success)}` : "暂无发送记录")));
    button.append(identity,status,el("span", "chevron", "›")); row.append(button);
    if (!account.has_account) {
      const connect = el("button", "", "连接 Trakt"); connect.disabled = busy || !account.configured;
      connect.onclick = action(async () => { await selectMember(account.member_id); await connectAccount(); }); row.append(connect);
    }
    container.append(row);
  }
}
function renderRecords() {
  const container = $("records");
  const open = new Set([...container.querySelectorAll("details[open]")].map(node => node.dataset.id));
  container.replaceChildren();
  const visible = records.filter(row => ActivityLogic.matches(row, filter, activityType));
  if (!visible.length) {
    const empty = el("div", "empty");
    empty.append(el("strong", "", "当前筛选下没有记录"), el("p", "", activityType === "important" ? "默认显示观看标记与收藏；可切换活动类型查看开始播放、进度和停止记录。" : "可切换活动类型或发送状态；新的记录会自动更新。"));
    container.append(empty); return;
  }
  for (const row of visible) {
    const details = el("details", "delivery"); details.dataset.id = row.id; details.open = open.has(row.id);
    const summary = el("summary");
    summary.append(el("span", `delivery-icon ${row.state}`, row.state === "sent" ? "✓" : ["failed","blocked"].includes(row.state) ? "!" : row.state === "skipped" ? "−" : "↗"));
    const main = el("div", "delivery-main");
    const episode = row.season != null && row.episode != null ? ` · S${String(row.season).padStart(2,"0")}E${String(row.episode).padStart(2,"0")}` : "";
    main.append(el("div", "delivery-title", (row.title || "未命名影片") + episode), el("div", `delivery-meta${["failed","blocked"].includes(row.state) ? " has-error" : ""}`, row.reason || events[row.event] || "播放同步"));
    const end = el("div", "delivery-end");
    end.append(el("span", `badge ${row.state}`, states[row.state] || row.state), el("p", "delivery-time", time(row.completed_at || row.occurred_at)));
    summary.append(main,end,el("span", "chevron", "›")); details.append(summary);
    const body = el("div", "delivery-detail");
    body.append(el("p", "", `${events[row.event] || "播放同步"} · 观看时间 ${time(row.occurred_at)}`));
    if (row.completed_at) body.append(el("p", "", `处理时间 ${time(row.completed_at)}`));
    if (row.error_status != null) body.append(el("p", "", row.error_status ? `Trakt 返回 HTTP ${row.error_status}` : "未能连接到 Trakt"));
    if (row.retry_at && ["retrying","blocked","failed"].includes(row.state)) body.append(el("p", "", `下次自动尝试 ${time(row.retry_at)}`));
    if (row.can_retry) {
      const retry = el("button", "", "核对后重试");
      retry.onclick = action(async () => { retry.disabled = true; try { const result = await api(row.transfer ? `actions/${encodeURIComponent(row.id)}/resume` : `retry/${encodeURIComponent(row.id)}`, "POST"); await refresh(); if (row.transfer) notice("已继续此操作的待发送任务。"); else retryNotice(result); } finally { retry.disabled = false; } });
      body.append(retry);
      if (row.can_repair) {
        const form = el("div", "repair-controls");
        const season = el("input"), episode = el("input");
        season.type = episode.type = "number"; season.min = "0"; episode.min = "1";
        season.value = row.season ?? ""; episode.value = row.episode ?? "";
        season.setAttribute("aria-label", "修正季号"); episode.setAttribute("aria-label", "修正集号");
        const repair = el("button", "", "修正季集并核对");
        repair.onclick = action(async () => {
          const s = Number(season.value), e = Number(episode.value);
          if (!season.value || !episode.value || !Number.isInteger(s) || !Number.isInteger(e) || s < 0 || e < 1) throw new Error("请输入有效的季号和集号");
          const result = await api(`retry/${encodeURIComponent(row.id)}`, "POST", selected, {season:s, episode:e});
          await refresh(); retryNotice(result);
        });
        form.append(el("span", "", "季"), season, el("span", "", "集"), episode, repair); body.append(form);
      }
      const dismiss = el("button", "", "忽略此记录");
      dismiss.onclick = action(async () => {
        if (!confirm("忽略此条失败记录？不会补传，也不会删除 Trakt 的观看历史。")) return;
        await api(row.transfer ? `actions/${encodeURIComponent(row.id)}/cancel` : `dismiss/${encodeURIComponent(row.id)}`, "POST"); await refresh();
      });
      body.append(dismiss);
    }
    if (row.state === "blocked") {
      const manage = el("button", "", "管理账号连接"); manage.onclick = () => { $("advanced").open = true; $("advanced").scrollIntoView({behavior:"smooth", block:"nearest"}); }; body.append(manage);
    }
    details.append(body); container.append(details);
  }
}
async function refresh() {
  const id = selected, own = ++revision;
  const [overview, detail, profile] = await Promise.all([api("accounts"), api("activity", "GET", id), api("profile", "GET", id)]);
  if (id !== selected || own !== revision) return;
  const accounts = overview.accounts;
  let current = accounts.find(account => account.member_id === id);
  if (!current) { current = await api("status", "GET", id); if (id !== selected || own !== revision) return; accounts.push(current); }
  const connected = accounts.filter(account => account.connected).length;
  const pending = accounts.reduce((sum, account) => sum + account.pending,0);
  const failed = accounts.reduce((sum, account) => sum + account.failed,0);
  const attention = accounts.some(account => account.last_error || account.warning || account.needs_reconnect);
  $("summary").textContent = failed ? `${failed} 条保留的失败记录 · ${connected} 个账号已连接` : attention ? `有账号需要检查 · ${pending} 条待发送` : connected ? `同步正常 · ${connected} 个账号已连接${pending ? ` · ${pending} 条待发送` : ""}` : "连接 Trakt 后，自动同步观看记录";
  $("summary").classList.toggle("attention", !!failed || attention);
  $("summary").classList.toggle("idle", !connected && !attention);
  $("account-count").textContent = `${connected} 个已连接`;
  $("activity-owner").textContent = `${current.username || memberLabel(id)}${current.username ? ` · ${memberLabel(id)}` : ""}的发送情况`;
  $("retry").hidden = !current.failed;
  $("acknowledge-dropped").hidden = !current.unacknowledged_dropped;
  $("connect").disabled = busy || !current.configured || current.has_account;
  $("disconnect").disabled = !current.has_account || busy;
  $("profile-owner").textContent = `${memberLabel(id)}的个人设置`;
  $("history-link").href = `history.html?member_id=${id}`;
  if (!savingProfile) for (const input of document.querySelectorAll("[data-preference]")) input.checked = profile.preferences[input.dataset.preference];
  records = detail.records;
  const nextAccounts = JSON.stringify([id,busy,accounts]), nextRecords = JSON.stringify([id,records]);
  if (nextAccounts !== accountSnapshot) { renderAccounts(accounts); accountSnapshot = nextAccounts; }
  if (nextRecords !== recordSnapshot) { renderRecords(); recordSnapshot = nextRecords; }
  $("updated").textContent = ` 更新于 ${new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}`;
}
async function connectAccount() {
  if (busy) return;
  const id = selected, own = ++generation; busy = true;
  $("auth-member").textContent = `为${memberLabel(id)}连接账号`;
  $("code").textContent = "…"; $("verify").removeAttribute("href");
  $("auth-state").textContent = "正在生成授权码…"; $("copy-code").disabled = true;
  $("authorization").showModal();
  try {
    const auth = await api("auth/begin", "POST", id);
    if (own !== generation) return;
    const url = new URL(auth.verification_url);
    if (url.protocol !== "https:" || !["trakt.tv","app.trakt.tv","auth.trakt.tv"].includes(url.hostname)) throw new Error("授权地址无效");
    $("verify").href = url.href; $("code").textContent = auth.user_code; $("copy-code").disabled = false;
    $("auth-state").textContent = "等待你在 Trakt 确认授权，完成后会自动连接。";
    let interval = Math.max(5, auth.interval);
    while (own === generation && Date.now()/1000 < auth.expires_at) {
      await new Promise(resolve => setTimeout(resolve, interval*1000));
      if (own !== generation) return;
      const state = await api("auth/poll", "POST", id);
      if (own !== generation) return;
      if (state.state === "connected") { $("authorization").close(); await refresh(); return; }
      if (["expired","denied","idle"].includes(state.state)) { $("auth-state").textContent = "授权已结束，请关闭窗口后重新连接。"; return; }
      interval = Math.max(5, state.interval || interval);
    }
    if (own === generation) $("auth-state").textContent = "授权码已过期，请关闭窗口后重新连接。";
  } catch (error) { if (own === generation) $("auth-state").textContent = error.message; }
  finally { if (own === generation) { busy = false; await refresh().catch(error => showError(error.message)); } }
}
$("refresh-all").onclick = action(refresh);
$("refresh").onclick = action(() => selectMember(readMember()));
$("member").onkeydown = event => { if (event.key === "Enter") $("refresh").click(); };
$("member").onchange = action(() => selectMember(readMember()));
$("add-account").onclick = () => { $("advanced").open = true; $("member").focus(); $("advanced").scrollIntoView({behavior:"smooth",block:"nearest"}); };
$("retry").onclick = action(async () => { const result = await api("retry", "POST"); await refresh(); retryNotice(result); });
$("acknowledge-dropped").onclick = action(async () => {
  if (!confirm("确认已知这些记录未同步？此操作只清除提示，不会补发记录，累计数量仍保留。")) return;
  await api("acknowledge-dropped", "POST"); await refresh(); notice("已清除提示，累计未同步数量仍保留。");
});
$("connect").onclick = action(async () => { await selectMember(readMember()); await connectAccount(); });
function closeAuth() { cancelAuth(); action(refresh)(); }
$("cancel-auth").onclick = closeAuth;
$("authorization").addEventListener("cancel", closeAuth);
$("copy-code").onclick = action(async () => {
  if (navigator.clipboard) await navigator.clipboard.writeText($("code").textContent);
  else {
    const range = document.createRange(); range.selectNodeContents($("code"));
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    if (!document.execCommand("copy")) { $("auth-state").textContent = "请长按或选中上方授权码进行复制。"; return; }
    selection.removeAllRanges();
  }
  $("copy-code").textContent = "已复制"; setTimeout(() => { $("copy-code").textContent = "复制代码"; },1500);
});
$("disconnect").onclick = action(async () => {
  await selectMember(readMember());
  if (!confirm(`断开${memberLabel(selected)}的 Trakt？本地待同步和近期发送记录将清除，Trakt 已有观看历史保留。`)) return;
  cancelAuth(); const result = await api("disconnect", "POST"); await refresh();
  if (!result.remote_revocation_confirmed) showError("本地已断开；请在 Trakt 已连接应用中确认撤销授权。");
});
$("activity-type").onchange = () => { activityType = $("activity-type").value; renderRecords(); };
const tabs = [...document.querySelectorAll("[data-filter]")];
for (const tab of tabs) {
  tab.onclick = () => { filter = tab.dataset.filter; for (const other of tabs) other.setAttribute("aria-selected", String(other === tab)); renderRecords(); };
  tab.onkeydown = event => { if (["ArrowLeft","ArrowRight"].includes(event.key)) { event.preventDefault(); const next = tabs[(tabs.indexOf(tab)+(event.key === "ArrowRight" ? 1 : tabs.length-1))%tabs.length]; next.click(); next.focus(); } };
}
for (const input of document.querySelectorAll("[data-preference]")) input.onchange = action(async () => {
  const id = selected;
  const inputs = [...document.querySelectorAll("[data-preference]")];
  const payload = Object.fromEntries(inputs.map(box => [box.dataset.preference, box.checked]));
  savingProfile = true; inputs.forEach(box => { box.disabled = true; });
  try { await api("profile", "PUT", id, payload); $("profile-state").textContent = "已保存到当前成员的资料"; }
  catch (error) { input.checked = !input.checked; throw error; }
  finally { savingProfile = false; inputs.forEach(box => { box.disabled = false; }); }
});
action(async () => {
  const viewer = await api("viewer"); const requested = Number(new URLSearchParams(location.search).get("member_id")); selected = viewer.is_admin && Number.isSafeInteger(requested) && requested >= 0 ? requested : viewer.member_id; $("member").value = selected;
  if (!viewer.is_admin) {
    $("add-account").hidden = true; $("member").hidden = true; $("refresh").hidden = true;
    document.querySelector('label[for="member"]').hidden = true;
  }
  await refresh();
})();
setInterval(() => { if (!document.hidden) refresh().then(() => showError("")).catch(error => showError(`状态暂未更新：${error.message}`)); },10000);

function retryNotice(result) {
  notice(result.retried ? `已将 ${result.retried} 条记录加入核对队列，将先检查 Trakt 历史再补传。` : "已核对：无需补传或仍待修正，请查看记录详情。");
}

/* 飞牛影视管理页逻辑。判断类函数都放这里，便于用 node 单测。 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  // ───────────────────────── 纯函数（见 logic.js，node 可直接单测） ─────────────────────────
  const {
    relTime, humanBytes, libStatText, categoryLabel,
    progressPct, alignSummary, mkNeedsWork,
  } = window.TrimLogic;

  // ───────────────────────── 页面逻辑 ─────────────────────────

  const API = "/api/v1/plugins/trim-media";

  function toast(msg, kind) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (kind ? " " + kind : "");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), 3500);
  }

  async function api(path, opts) {
    const resp = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || body.message || `HTTP ${resp.status}`);
    return body.data !== undefined ? body.data : body;
  }

  function busy(btn, on, label) {
    if (on) { btn._text = btn.textContent; btn.textContent = label || "处理中…"; btn.disabled = true; }
    else { btn.textContent = btn._text || btn.textContent; btn.disabled = false; }
  }

  // ── 片头片尾 ──
  const MK_STATE_TEXT = { same: "一致", diff: "不一致", unset: "未设置", trim_only: "仅飞牛有" };
  const MK_STATE_CLASS = { same: "ok", diff: "warn", unset: "", trim_only: "" };
  const MK_ROWS = [];            // 最近一次总览数据
  // 折叠状态跨渲染保持：默认「一致 / 仅飞牛有」收起，用户手动点过的记住
  const MK_OPENED = new Set();   // 手动展开的（默认收起的组）
  const MK_CLOSED = new Set();   // 手动收起的（默认展开的组）
  const MK_PAGE = { shown: 60, size: 60 };  // 分批渲染，避免几百行一次画完卡顿
  let MK_FILTER = "all";
  let MK_QUERY = "";

  function fmtSec(v) {
    if (v === null || v === undefined) return "—";
    const s = Number(v);
    if (!Number.isFinite(s) || s <= 0) return "—";
    const m = Math.floor(s / 60), r = s % 60;
    return m ? `${m}分${r ? r + "秒" : ""}` : `${r}秒`;
  }

  function mkMatch(r) {
    if (MK_QUERY && !(r.title || "").toLowerCase().includes(MK_QUERY)) return false;
    if (MK_FILTER === "todo") return mkNeedsWork(r);
    if (MK_FILTER !== "all" && r.state !== MK_FILTER) return false;
    return true;
  }

  function mkRowHtml(r) {
    const cls = MK_STATE_CLASS[r.state] || "";
    const mcPart = r.mc_opening === null
      ? `<span class="muted">未识别</span>`
      : `片头 ${fmtSec(r.mc_opening)} / 片尾 ${fmtSec(r.mc_ending)}`;
    const trimPart = (r.trim_opening === null && r.trim_ending === null)
      ? `<span class="muted">未设置</span>`
      : `片头 ${fmtSec(r.trim_opening)} / 片尾 ${fmtSec(r.trim_ending)}`;
    const key = `${r.library_id}|${r.tmdb_id}|${r.season}`;
    const canPush = r.mc_opening !== null && r.state !== "same";
    return `
      <td class="mk-title">${escapeHtml(r.title || "—")} <span class="tag">S${String(r.season).padStart(2, "0")}</span>${r.episodes ? ` <span class="muted">${r.episodes} 集</span>` : ""}</td>
      <td class="mk-num">${mcPart}</td>
      <td class="mk-num">${trimPart}</td>
      <td><span class="tag ${cls}">${MK_STATE_TEXT[r.state] || r.state}</span></td>
      <td class="acts">
        <button class="btn btn-sm" data-mk="push" data-key="${key}" ${canPush ? "" : "disabled"}>推 MC 值</button>
        <button class="btn btn-sm" data-mk="edit" data-key="${key}">修改</button>
      </td>`;
  }

  /** 按当前筛选/搜索重画表格（分组、折叠、分批都在这里）。 */
  function renderMarkers() {
    const body = $("#mk-body");
    const rows = MK_ROWS.filter(mkMatch);
    // 统计永远按全量算，不受筛选影响
    const counts = { same: 0, diff: 0, unset: 0, trim_only: 0 };
    for (const r of MK_ROWS) counts[r.state] = (counts[r.state] || 0) + 1;

    const tag = $("#mk-state");
    tag.textContent = `${MK_ROWS.length} 季 · 待处理 ${counts.diff + counts.unset}`;
    tag.className = "tag " + (counts.diff || counts.unset ? "warn" : "ok");
    $("#mk-count").textContent = rows.length === MK_ROWS.length
      ? `${MK_ROWS.length} 季`
      : `筛选出 ${rows.length} / ${MK_ROWS.length} 季`;
    $("#mk-filters").querySelectorAll("[data-mkfilter]").forEach((b) =>
      b.classList.toggle("on", b.dataset.mkfilter === MK_FILTER)
    );

    if (!MK_ROWS.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">两边都没有季数据（先同步一次，或确认飞牛侧有剧集库）</td></tr>`;
      $("#mk-more").innerHTML = "";
      return;
    }
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">没有匹配的季</td></tr>`;
      $("#mk-more").innerHTML = "";
      return;
    }

    // 分组：库 → 状态；一次最多画 MK_PAGE.shown 行，剩下用「显示更多」
    const groups = [];
    const byLib = new Map();
    for (const r of rows) {
      const k = r.library || "（未知库）";
      if (!byLib.has(k)) byLib.set(k, []);
      byLib.get(k).push(r);
    }
    let budget = MK_PAGE.shown;
    let drawn = 0;
    for (const [libName, list] of byLib) {
      for (const state of ["diff", "unset", "trim_only", "same"]) {
        const items = list.filter((r) => r.state === state);
        if (!items.length) continue;
        const take = budget > 0 ? items.slice(0, budget) : [];
        budget -= take.length;
        drawn += take.length;
        // 折叠的组不画行，但它的行也算「还没显示」——展开后重新走一遍预算
        groups.push({ libName, state, items: take, more: items.length - take.length });
      }
    }

    body.innerHTML = "";
    for (const g of groups) {
      const key = `${g.libName}::${g.state}`;
      const foldedByDefault = g.state === "same" || g.state === "trim_only";
      const collapsed = foldedByDefault ? !MK_OPENED.has(key) : MK_CLOSED.has(key);
      const head = document.createElement("tr");
      head.className = "mk-group";
      head.innerHTML = `
        <td colspan="5">
          <b>${escapeHtml(g.libName)}</b> · ${MK_STATE_TEXT[g.state]}
          <span class="tag ${MK_STATE_CLASS[g.state] || ""}">${g.items.length + g.more} 季</span>
          <span class="mk-caret">${collapsed ? "▸" : "▾"}</span>
        </td>`;
      head.addEventListener("click", () => {
        if (collapsed) MK_OPENED.add(key); else MK_CLOSED.add(key);
        if (collapsed) MK_CLOSED.delete(key); else MK_OPENED.delete(key);
        renderMarkers();
      });
      body.appendChild(head);
      if (collapsed) continue;
      for (const r of g.items) {
        const tr = document.createElement("tr");
        tr.className = "mk-row";
        tr.innerHTML = mkRowHtml(r);
        body.appendChild(tr);
      }
    }
    // 行分批的「显示更多」：按预算算没画出来的行数（不靠 DOM 反查，折叠组也算）
    const more = $("#mk-more");
    const hidden = rows.length - drawn;
    more.innerHTML = hidden > 0
      ? `<button class="btn btn-sm" id="mk-show-more">再显示 ${Math.min(MK_PAGE.size, hidden)} 季（还有 ${hidden} 季）</button>`
      : "";
    const btn = $("#mk-show-more");
    if (btn) btn.addEventListener("click", () => { MK_PAGE.shown += MK_PAGE.size; renderMarkers(); });

    bindMarkerActions();
  }

  function bindMarkerActions() {
    const body = $("#mk-body");
    body.querySelectorAll('[data-mk="push"]').forEach((b) =>
      b.addEventListener("click", async () => {
        const row = MK_ROWS.find((x) => `${x.library_id}|${x.tmdb_id}|${x.season}` === b.dataset.key);
        if (!row) return;
        busy(b, true, "…");
        try {
          const res = await api("/markers/push", {
            method: "POST",
            body: JSON.stringify({ library_id: row.library_id, tmdb_id: row.tmdb_id, season: row.season }),
          });
          if (res.failed) {
            toast(`推送失败 ${res.failed} 季，看运行日志`, "bad");
          } else if (res.written) {
            toast(`「${row.title} S${row.season}」已写入飞牛`, "ok");
          } else {
            toast(`「${row.title} S${row.season}」值没变，跳过`, "");
          }
          // 本地更新这一行，不整表重刷（省一次十几秒的读飞牛）
          if (res.written) {
            row.trim_opening = row.mc_opening;
            row.trim_ending = row.mc_ending;
            row.state = "same";
            renderMarkers();
          }
        } catch (e) { toast(e.message, "bad"); }
        finally { busy(b, false); }
      })
    );
    body.querySelectorAll('[data-mk="edit"]').forEach((b) =>
      b.addEventListener("click", () => {
        const row = MK_ROWS.find((x) => `${x.library_id}|${x.tmdb_id}|${x.season}` === b.dataset.key);
        if (row) openMarkerDialog(row);
      })
    );
  }

  async function loadMarkers(refresh) {
    const tag = $("#mk-state");
    tag.textContent = refresh ? "读飞牛中…" : "读缓存…";
    tag.className = "tag";
    try {
      const r = await api("/markers" + (refresh ? "?refresh=true" : ""));
      MK_ROWS.length = 0;
      MK_ROWS.push(...(r.seasons || []));
      MK_PAGE.shown = MK_PAGE.size;   // 换数据回到第一屏
      renderMarkers();
      if (r.from_cache && r.checked_at) {
        $("#mk-count").textContent += ` · 缓存于 ${relTime(r.checked_at)}`;
      }
    } catch (err) {
      tag.textContent = "加载失败";
      tag.className = "tag bad";
      $("#mk-body").innerHTML = `<tr><td colspan="5" class="empty">${escapeHtml(err.message)}</td></tr>`;
    }
  }

  function openMarkerDialog(row) {
    const dlg = $("#dlg-marker");
    $("#mk-dlg-title").textContent = `${row.title || "—"} · 第 ${row.season} 季`;
    $("#mk-dlg-meta").textContent =
      `MC 识别：${row.mc_opening === null ? "未识别" : `片头 ${fmtSec(row.mc_opening)} / 片尾 ${fmtSec(row.mc_ending)}`} · ` +
      `飞牛当前：${row.trim_opening === null && row.trim_ending === null ? "未设置" : `片头 ${fmtSec(row.trim_opening)} / 片尾 ${fmtSec(row.trim_ending)}`}`;
    const o = $("#mk-dlg-opening"), e = $("#mk-dlg-ending");
    o.value = row.trim_opening ?? ""; e.value = row.trim_ending ?? "";
    o.placeholder = row.mc_opening ?? "秒"; e.placeholder = row.mc_ending ?? "秒";
    dlg.showModal();
    $("#mk-dlg-ok").onclick = async (ev) => {
      ev.preventDefault();
      const payload = {
        library_id: row.library_id, tmdb_id: row.tmdb_id, season: row.season,
        opening: o.value === "" ? null : Number(o.value),
        ending: e.value === "" ? null : Number(e.value),
      };
      try {
        const res = await api("/markers/season", { method: "POST", body: JSON.stringify(payload) });
        toast(`已写入：片头 ${fmtSec(res.skip_opening)} / 片尾 ${fmtSec(res.skip_ending)}`, "ok");
        dlg.close(); loadMarkers();
      } catch (err) { toast(err.message, "bad"); }
    };
    $("#mk-dlg-reset").onclick = async (ev) => {
      ev.preventDefault();
      if (!confirm(`清除「${row.title}」第 ${row.season} 季的片头片尾？\n\n飞牛不支持写 0 清除，会写入 1 秒顶掉旧值（实际等于不跳）。`)) return;
      try {
        await api("/markers/season", {
          method: "POST",
          body: JSON.stringify({ library_id: row.library_id, tmdb_id: row.tmdb_id, season: row.season, reset: true }),
        });
        toast("已重置", "ok"); dlg.close(); loadMarkers();
      } catch (err) { toast(err.message, "bad"); }
    };
  }

  // ── 状态条 ──
  async function loadStatus() {
    try {
      const st = await api("/status");
      $("#st-conn").textContent = st.configured ? "已配置" : "未配置";
      // 地址只显示主机:端口，协议前缀在状态条里没意义（另一格已经表明是飞牛服务）
      $("#st-host").textContent = (st.base_url || "").replace(/^https?:\/\//i, "") || "—";
      $("#st-host").title = st.base_url || "";
      $("#st-libs").textContent = (st.libraries || []).length + " 个";
      $("#st-sync").textContent = relTime(st.last_sync_at);
      // 「同步成员」：进度的两边对账是谁的（飞牛按账号存进度，这里说清楚）
      const mName = st.member_username || "超管本人";
      const mMiss = Boolean(st.member_username) && st.member_resolved === null;
      $("#st-member").textContent = mMiss ? `${mName}（没找到）` : mName;
      $("#st-tls").textContent = st.verify_tls ? "校验" : "已关闭";
      const ver = st.versions || {};
      // 前后端各自一格：前端 = 影视网页，服务端 = mediasrv
      $("#st-ver-web").textContent = ver.web || "—";
      $("#st-ver-server").textContent = ver.server || "—";
      $("#conn-dot").className = "dot " + (st.configured ? "ok" : "bad");
    } catch (err) {
      $("#st-conn").textContent = "加载失败";
      $("#conn-dot").className = "dot bad";
    }
  }

  // ── 媒体库映射 ──
  async function loadLibraries() {
    try {
      const st = await api("/status");
      const list = $("#lib-list");
      list.innerHTML = "";
      const libs = st.libraries || [];
      if (!libs.length) {
        list.innerHTML = `<div class="empty" style="grid-column:1/-1">还没有接入的库，点右上「接入飞牛库」</div>`;
        return;
      }
      for (const lib of libs) {
        const card = document.createElement("div");
        card.className = "card";
        const mode = "本机直连";
        const modeTag = `<span class="tag ok">本地库</span>`;
        card.innerHTML = `
          <div class="row">
            <span class="name">${escapeHtml(lib.name)} ${modeTag}</span>
            <span class="kind">${lib.kind === "movie" ? "电影" : "剧集"}</span>
          </div>
          <div class="meta">
            飞牛库：<b>${escapeHtml(lib.fn_name || lib.fn_guid || "—")}</b><br>
            内容：<b>${escapeHtml(libStatText(lib.stats))}</b> · ${mode}<br>
            片头片尾：<b>${lib.markers_pushed ? `${lib.markers_pushed} 季已写回飞牛` : "尚未写回"}</b><br>
            最近同步：<b>${relTime(lib.last_sync_at)}</b>
          </div>
          ${coverageBlock(lib.coverage)}
          <div class="acts">
            <button class="btn btn-sm" data-act="sync-one" data-id="${lib.id}">同步</button>
            <button class="btn btn-sm" data-act="cov" data-id="${lib.id}">检查覆盖</button>
            <button class="btn btn-sm btn-danger" data-act="del" data-id="${lib.id}" data-name="${escapeHtml(lib.name)}">移除库</button>
          </div>`;
        list.appendChild(card);
      }
      renderSyncSummary(st.last_sync_result);
      list.querySelectorAll('[data-act="sync-one"]').forEach((b) =>
        b.addEventListener("click", async () => {
          busy(b, true, "…");
          try { await api("/sync-now", { method: "POST", body: "{}" }); toast("已加入同步队列", "ok"); }
          catch (e) { toast(e.message, "bad"); }
          busy(b, false);
        })
      );
      list.querySelectorAll('[data-act="cov"]').forEach((b) =>
        b.addEventListener("click", async () => {
          busy(b, true, "统计中…");
          try {
            const r = await api(`/check-coverage?library_id=${Number(b.dataset.id)}`);
            const first = (r.libraries || [])[0];
            if (first) {
              toast(
                first.uncovered
                  ? `「${first.library}」漏 ${first.uncovered} 个文件（已收 ${first.covered}/${first.files}）`
                  : `「${first.library}」${first.files} 个文件全部收录`,
                first.uncovered ? "bad" : "ok"
              );
            } else {
              toast("这个库没有本机根，无需检查", "bad");
            }
            loadLibraries();
          } catch (err) {
            toast("检查失败：" + err.message, "bad");
          } finally {
            busy(b, false);
          }
        })
      );
      list.querySelectorAll('[data-act="del"]').forEach((b) =>
        b.addEventListener("click", async () => {
          const id = b.dataset.id, name = b.dataset.name;
          if (!confirm(`删除 MC 库「${name}」？\n\n只删库和台账，不动磁盘文件、不动飞牛。`)) return;
          try {
            await fetch(`/api/v1/libraries/${id}`, { method: "DELETE" });
            toast("已删除", "ok");
            loadLibraries(); loadStatus();
          } catch (e) { toast("删除失败：" + e.message, "bad"); }
        })
      );
    } catch (err) { toast("加载库失败：" + err.message, "bad"); }
  }

  /** 上一轮同步的结果：新接入了哪些库、有没有错。
   *  新架构下库是普通的本地库，同步只做「发现新库 → 建库 → 触发扫描」，
   *  条目级别的进度不在这里报（那走播放事件与片头片尾页）。 */
  function renderSyncSummary(result) {
    const box = $("#sync-summary");
    if (!box) return;
    if (!result || !result.at) {
      box.innerHTML = "";
      return;
    }
    const chips = [];
    for (const row of result.claimed || []) {
      chips.push(`<span class="tag ok">${escapeHtml(row.name || "")}：认领已有库（记录不动）</span>`);
    }
    for (const row of result.adopted || []) {
      chips.push(`<span class="tag ok">${escapeHtml(row.name || "")}：已接入，正在扫描</span>`);
    }
    for (const err of result.errors || []) {
      chips.push(`<span class="tag bad">${escapeHtml(String(err).slice(0, 80))}</span>`);
    }
    if (!chips.length) chips.push(`<span class="tag">没有新库</span>`);
    box.innerHTML = `<div class="hint" style="margin:10px 0 0">上次同步 ${relTime(result.at)}：${chips.join(" ")}</div>`;
  }

  // ── 飞牛侧库 ──
  async function loadFnLibraries() {
    let libs = [];
    try {
      const [remote, locals] = await Promise.all([api("/libraries"), api("/status")]);
      const localByGuid = new Set((locals.libraries || []).map((l) => l.fn_guid || l.fn_guid_text).filter(Boolean));
      const knownNames = new Set((locals.libraries || []).map((l) => (l.fn_name || "").trim()));
      const body = $("#fn-libs-body");
      body.innerHTML = "";
      libs = remote || [];
      $("#fn-libs-count").textContent = libs.length + " 个";
      if (!libs.length) { body.innerHTML = `<tr><td colspan="4" class="empty">飞牛上没有媒体库</td></tr>`; return libs; }
      for (const lib of libs) {
        const imported = localByGuid.has(lib.guid) || knownNames.has(lib.name);
        const tr = document.createElement("tr");
        // 目录往往又长又多：一行一个、最多显示两行，超出省略；悬停看完整路径
        const dirs = (lib.dirs || []).map((d) => escapeHtml(String(d).replace(/\/+$/, "")));
        const dirHtml = dirs.length ? dirs.join("<br>") : "—";
        tr.innerHTML = `
          <td class="name">${escapeHtml(lib.name)}</td>
          <td class="kind">${categoryLabel(lib.category)}</td>
          <td class="path"><code title="${escapeHtml((lib.dirs || []).join("；"))}">${dirHtml}</code></td>
          <td class="state">${imported
            ? `<span class="tag ok">已接入</span>`
            : `<span class="tag">未接入</span>`}</td>`;
        body.appendChild(tr);
      }
    } catch (err) {
      $("#fn-libs-body").innerHTML = `<tr><td colspan="4" class="empty">加载失败：${escapeHtml(err.message)}</td></tr>`;
    }
    return libs;
  }

  // ── 待识别 ──
  async function loadUnidentified() {
    try {
      const st = await api("/status");
      const libIds = new Set((st.libraries || []).map((l) => l.id));
      // 分组数据只带 library_id，类型要从库清单里查（电影/剧集的认领前缀不同）
      const libKind = new Map((st.libraries || []).map((l) => [l.id, l.kind]));
      const resp = await fetch("/api/v1/libraries/identification/unidentified-files");
      const payload = await resp.json().catch(() => null);
      const groups = ((payload && payload.data) || []).filter((g) => libIds.has(g.library_id));
      const list = $("#unid-list");
      list.innerHTML = "";
      const total = groups.reduce((n, g) => n + (g.file_count || 0), 0);
      $("#unid-count").textContent = total;

      if (!groups.length) {
        list.innerHTML = `<div class="empty">全部识别完毕，没有待认领的内容</div>`;
        return;
      }
      for (const g of groups) {
        // 每组一张卡：组名 / 库 / 文件数与体积 / 失败原因 / 候选建议 / 文件明细
        const card = document.createElement("div");
        card.className = "card unid-card";
        const cands = (g.candidates || [])
          .map(
            (c) => `<div class="cand">
              <b>${escapeHtml(c.title)}</b>${c.year ? ` <span class="d">(${c.year})</span>` : ""}
              ${c.episode_count ? `<span class="d">· ${c.episode_count} 集</span>` : ""}
              ${c.tmdb_id ? `<a target="_top" href="https://www.themoviedb.org/${libKind.get(g.library_id) === "movie" ? "movie" : "tv"}/${c.tmdb_id}">TMDB ${c.tmdb_id}</a>` : ""}
            </div>`
          )
          .join("");
        const files = (g.files || [])
          .map(
            (f) => `<div class="file">
              <span class="t">${escapeHtml((f.file_path || "").split("/").pop() || f.file_name || "")}</span>
              <span class="d">${humanBytes(f.size_bytes || 0)}${f.duration_seconds ? ` · ${Math.round(f.duration_seconds / 60)} 分钟` : ""}</span>
            </div>`
          )
          .join("");
        card.innerHTML = `
          <div class="row">
            <span class="name">${escapeHtml(g.label)}
              <span class="tag warn">${escapeHtml(g.code || "待认领")}</span>
            </span>
            <span class="tag">${escapeHtml(g.library_name)} · ${g.file_count} 文件 · ${humanBytes(g.total_size_bytes)}</span>
          </div>
          ${g.reason ? `<div class="meta">${escapeHtml(g.reason)}</div>` : ""}
          ${cands ? `<div class="meta"><b>候选（点击整组认领）：</b>${cands}</div>` : ""}
          ${files ? `<details class="files"><summary>文件明细（${(g.files || []).length}）</summary>${files}</details>` : ""}
          <div class="acts">
            ${g.candidates && g.candidates.length && g.candidates[0].tmdb_id ? `<button class="btn btn-sm" data-claim="1" data-kind="${escapeHtml(libKind.get(g.library_id) || "tv")}" data-files="${escapeHtml(JSON.stringify((g.files || []).map((f) => f.id)))}" data-tmdb="${g.candidates[0].tmdb_id}" data-title="${escapeHtml((g.candidates[0].title || "").slice(0, 20))}">一键认领为「${escapeHtml((g.candidates[0].title || "").slice(0, 18))}」</button>` : ""}
            <a class="btn btn-sm" target="_top" href="/library/${g.library_id}">去媒体库处理</a>
          </div>`;
        list.appendChild(card);
      }
      list.querySelectorAll("[data-claim]").forEach((b) =>
        b.addEventListener("click", async () => {
          let fileIds = [];
          try { fileIds = JSON.parse(b.dataset.files || "[]"); } catch { fileIds = []; }
          const tmdb = Number(b.dataset.tmdb);
          // 电影和剧集的 title_ref 前缀不同：写死 tmdb:tv: 的话，电影会被主线拒绝
          const mediaType = b.dataset.kind === "movie" ? "movie" : "tv";
          if (!fileIds.length || !tmdb) { toast("认领信息不完整", "bad"); return; }
          busy(b, true, "认领中…");
          try {
            const resp = await fetch("/api/v1/libraries/identification/file-title-assignments", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                file_ids: fileIds,
                title_ref: `tmdb:${mediaType}:${tmdb}`,
                season_number: null,
              }),
            });
            const body = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(body.detail || body.message || `HTTP ${resp.status}`);
            toast(`已认领 ${fileIds.length} 个文件为「${b.dataset.title}」`, "ok");
            loadUnidentified();
          } catch (err) {
            toast("认领失败：" + err.message, "bad");
          } finally {
            busy(b, false);
          }
        })
      );
    } catch (err) {
      $("#unid-list").innerHTML = `<div class="empty">加载失败：${escapeHtml(err.message)}</div>`;
    }
  }

  // ── 同步进度（每个库的可用文件比例） ──
  // （原「MC 成员」面板已移除：与飞牛账号面板重复，账号管理看下面 loadAccount）

  // ── 接入飞牛库弹窗 ──
  async function openAddDialog() {
    const sel = $("#add-lib-select");
    sel.innerHTML = `<option value="">加载中…</option>`;
    $("#add-lib-name").value = "";
    $("#add-lib-local").value = "";
    const dlg = $("#dlg-add");
    if (typeof dlg.showModal === "function") dlg.showModal();
    try {
      const libs = await api("/libraries");
      const usable = (libs || []).filter((l) => l.supported !== false && !l.overlap && !l.imported);
      sel.innerHTML = "";
      if (!usable.length) {
        sel.innerHTML = `<option value="">没有可接入的库</option>`;
        return;
      }
      for (const lib of usable) {
        const opt = document.createElement("option");
        opt.value = lib.guid;
        opt.dataset.kind = lib.category === "Movie" ? "movie" : "tv";
        opt.dataset.name = lib.name || "";
        opt.dataset.local = (lib.local_roots || [])[0] || "";
        opt.disabled = Boolean(lib.imported);
        opt.textContent = `${lib.name || lib.guid}（${categoryLabel(lib.category)}）${lib.imported ? " · 已接入" : ""}`;
        sel.appendChild(opt);
      }
      const pick = (guid) => {
        const opt = sel.selectedOptions[0];
        if (!opt) return;
        $("#add-lib-name").value = opt.dataset.name || "";
        $("#add-lib-local").value = opt.dataset.local || "";
      };
      sel.onchange = () => pick(sel.value);
      const first = usable.find((l) => !l.imported);
      if (first) sel.value = first.guid;
      pick(sel.value);
    } catch (err) {
      sel.innerHTML = `<option value="">加载失败：${escapeHtml(err.message)}</option>`;
    }
  }

  async function submitAddLib() {
    const sel = $("#add-lib-select");
    const guid = sel.value;
    if (!guid) { toast("先选一个飞牛库", "bad"); return; }
    const opt = sel.selectedOptions[0];
    const body = {
      library_guid: guid,
      library_name: opt ? opt.dataset.name || "" : "",
      name: $("#add-lib-name").value.trim() || (opt ? opt.dataset.name || "" : ""),
      kind: opt ? opt.dataset.kind || "movie" : "movie",
      local_root: $("#add-lib-local").value.trim(),
    };
    const btn = $("#add-lib-ok");
    busy(btn, true, "接入中…");
    try {
      const res = await api("/create-library", { method: "POST", body: JSON.stringify(body) });
      $("#dlg-add").close();
      toast(`已接入「${body.name}」${res && res.created === false ? "（同步已在进行）" : "，正在同步"}`, "ok");
      loadLibraries(); loadFnLibraries(); loadStatus();
    } catch (err) {
      toast("接入失败：" + err.message, "bad");
    } finally {
      busy(btn, false);
    }
  }

  // ── 飞牛账号（含每账号可见库权限管理） ──
  async function loadAccount(refresh) {
    try {
      const r = await api("/account" + (refresh ? "?refresh=true" : ""));
      const cur = r.current;
      const box = $("#acc-current");
      if (!cur) {
        box.innerHTML = `<div class="empty">没取到账号信息，检查连接配置</div>`;
      } else {
        const role = cur.is_admin ? "管理员" : "普通账号";
        const scope = cur.library_count ? `可见 ${cur.library_count} 个库` : "未分配库权限";
        const seen = cur.last_login_at ? `${relTime(cur.last_login_at)}登录` : "最近登录：未知";
        const from = (cur.sources || []).length
          ? `NAS 来源：${(cur.sources || []).join("、")}`
          : "";
        box.innerHTML = `
          <div class="row-item">
            <span class="t">${escapeHtml(cur.username)}</span>
            <span class="d">${role} · ${scope} · ${seen}${from ? ` · ${escapeHtml(from)}` : ""}</span>
            <span class="tag ${cur.is_admin ? "ok" : ""}">${role}</span>
          </div>`;
      }
      const rows = r.users || [];
      const isAdmin = !!r.is_admin;
      const state = $("#acc-state");
      state.textContent = rows.length ? `${rows.length} 个账号` : (isAdmin ? "没有账号" : "需要管理员");
      state.className = "tag " + (isAdmin ? "ok" : "warn");
      const wrap = $("#acc-users");
      wrap.innerHTML = "";
      if (!rows.length) {
        wrap.innerHTML = `<div class="empty" style="grid-column:1/-1">${
          isAdmin ? "飞牛上没有其他账号" : "当前账号不是管理员，看不到账号列表"
        }</div>`;
        return;
      }
      for (const u of rows) {
        const libs = u.libraries || [];
        const card = document.createElement("div");
        card.className = "card";
        const libTags = libs.length
          ? libs.map((l) => `<span class="tag">${escapeHtml(l.title || l)}</span>`).join(" ")
          : `<span class="tag bad">未分配库</span>`;
        const btnTitle = u.is_admin ? "管理员始终可见全部库" : (isAdmin ? "" : "需要插件账号是飞牛管理员");
        card.innerHTML = `
          <div class="row">
            <span class="name">${escapeHtml(u.username)}</span>
            <span class="tag ${u.is_admin ? "ok" : ""}">${u.is_admin ? "管理员" : "普通"}</span>
          </div>
          <div class="meta">${u.is_admin ? "可见全部库" : `可见 ${libs.length} 个库`} · ${u.last_login_at ? relTime(u.last_login_at) + "登录" : "从未登录"}</div>
          <div class="mk-libtags">${libTags}</div>
          <div class="acts">
            <button class="btn btn-sm" data-acc="perm" data-guid="${escapeHtml(u.guid || "")}" data-name="${escapeHtml(u.username)}" ${u.is_admin || !isAdmin ? "disabled" : ""} ${btnTitle ? `title="${btnTitle}"` : ""}>设置可见库</button>
          </div>`;
        wrap.appendChild(card);
      }
      wrap.querySelectorAll('[data-acc="perm"]').forEach((b) =>
        b.addEventListener("click", () => openPermDialog(r, b.dataset.guid, b.dataset.name))
      );
    } catch (err) {
      $("#acc-state").textContent = "加载失败";
      $("#acc-state").className = "tag bad";
    }
  }

  async function openPermDialog(data, guid, name) {
    const dlg = $("#dlg-perm");
    $("#perm-dlg-title").textContent = `${name} · 可见的媒体库`;
    let libs = [];
    try { libs = (await loadFnLibraries()) || []; } catch (e) { libs = []; }
    if (!libs.length) {
      toast("拿不到飞牛库清单，无法编辑", "bad");
      return;
    }
    const target = (data.users || []).find((u) => u.guid === guid) || {};
    // libraries 兼容两种形状：新格式 [{guid,title}] / 旧快照 ["库名"]。
    // 旧格式没有 guid，按库名对齐（/libraries 的 name 与飞牛标题一致）。
    const grantedGuids = new Set((target.libraries || []).map((l) => l.guid).filter(Boolean));
    const grantedNames = new Set((target.libraries || []).map((l) => l.title || l).filter(Boolean));
    const box = $("#perm-dlg-libs");
    box.innerHTML = "";
    for (const lib of libs) {
      const id = `perm-${lib.guid}`;
      const label = document.createElement("label");
      label.className = "perm-lib";
      label.innerHTML = `
        <input type="checkbox" id="${id}" value="${escapeHtml(lib.guid)}" ${(grantedGuids.has(lib.guid) || grantedNames.has(lib.name)) ? "checked" : ""}>
        <span>${escapeHtml(lib.name)}</span>
        <small>${escapeHtml(categoryLabel(lib.category))}${lib.imported ? " · 已接入 MC" : ""}</small>`;
      box.appendChild(label);
    }
    dlg.showModal();
    $("#perm-dlg-ok").onclick = async (ev) => {
      ev.preventDefault();
      const picked = [...box.querySelectorAll("input:checked")].map((i) => i.value);
      if (!picked.length) { toast("至少勾一个库（全不勾等于这个账号看不到任何内容）", "bad"); return; }
      busy($("#perm-dlg-ok"), true, "保存中…");
      try {
        await api("/account/library-access", {
          method: "POST",
          body: JSON.stringify({ guid, library_guids: picked }),
        });
        toast(`「${name}」的可见库已更新`, "ok");
        dlg.close(); loadAccount(true);
      } catch (err) { toast(err.message, "bad"); }
      finally { busy($("#perm-dlg-ok"), false); }
    };
  }

  $("#btn-acc-refresh").addEventListener("click", async (e) => {
    busy(e.target, true, "…");
    await loadAccount(true);
    busy(e.target, false);
  });

  // ── 运行日志（每 3 秒增量拉一次） ──
  const LOG_POLL_MS = 3000;
  let logPaused = false;
  let logLastKey = "";

  function logKey(item) {
    return `${item.ts}|${item.message}`;
  }

  function logLineHtml(item) {
    const level = String(item.level || "").toUpperCase();
    const cls = level === "ERROR" || level === "CRITICAL" ? "bad" : level === "WARNING" ? "warn" : "";
    const time = String(item.ts || "").slice(11) || String(item.ts || "");
    // 消息里常带「库「飞牛剧集（直连）」」这类中文书名号，原样显示即可
    return `<div class="logline ${cls}"><span class="t">${escapeHtml(time)}</span>`
      + `<span class="l">${escapeHtml(level)}</span>`
      + `<span class="m">${escapeHtml(item.message)}</span></div>`;
  }

  async function loadLogs() {
    if (logPaused) return;
    const box = $("#log-box");
    try {
      const r = await api("/runtime-logs?limit=200");
      const items = r.items || [];
      if (!items.length) {
        if (!logLastKey) box.innerHTML = `<div class="empty">还没有日志</div>`;
        $("#log-state").textContent = "无记录";
        return;
      }
      const lastKey = logLastKey;
      let start = 0;
      if (lastKey) {
        // 从后往前找上一条，命中它的下一条就是新增（同一秒同文案也不会错位）
        for (let i = items.length - 1; i >= 0; i--) {
          if (logKey(items[i]) === lastKey) { start = i + 1; break; }
        }
      }
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      if (!lastKey || start === 0) {
        box.innerHTML = items.map(logLineHtml).join("");
        box.scrollTop = box.scrollHeight;
      } else if (start < items.length) {
        box.insertAdjacentHTML("beforeend", items.slice(start).map(logLineHtml).join(""));
        if (atBottom) box.scrollTop = box.scrollHeight;
      }
      logLastKey = logKey(items[items.length - 1]);
      $("#log-state").textContent = `${items.length} 条`;
    } catch (err) {
      $("#log-state").textContent = "加载失败";
    }
  }

  $("#btn-log-toggle").addEventListener("click", (e) => {
    logPaused = !logPaused;
    e.target.textContent = logPaused ? "继续" : "暂停";
    e.target.classList.toggle("btn-primary", logPaused);
    if (!logPaused) loadLogs();
  });
  $("#btn-log-clear").addEventListener("click", () => {
    logLastKey = "";
    $("#log-box").innerHTML = `<div class="empty">已清屏，新日志会继续出现</div>`;
  });

  // ── 工具 ──
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── 启动 ──
  $("#btn-sync").addEventListener("click", async (e) => {
    busy(e.target, true, "同步中…");
    try {
      const r = await api("/sync-now", { method: "POST", body: "{}" });
      toast(r.created ? "已开始同步，任务在「活动」里" : "同步已在进行中", "ok");
      setTimeout(() => { loadLibraries(); loadStatus(); busy(e.target, false); }, 2500);
    } catch (err) { toast(err.message, "bad"); busy(e.target, false); }
  });
  $("#btn-align").addEventListener("click", async (e) => {
    busy(e.target, true, "对比中…");
    try {
      const r = await api("/align", { method: "POST", body: "{}" });
      const reports = r.libraries || [];
      const box = $("#sync-summary");
      if (box && reports.length) {
        box.innerHTML = `<div class="hint" style="margin:10px 0 0">差异检查：`
          + reports.map((x) => `<span class="tag ${x.error ? "bad" : ""}">${escapeHtml(x.library || "—")}：${escapeHtml(alignSummary(x))}</span>`).join(" ")
          + `</div>`;
      }
      const missMc = reports.reduce((n, x) => n + ((x.missing_in_mc || []).length), 0);
      const missTrim = reports.reduce((n, x) => n + ((x.missing_in_trim || []).length), 0);
      toast(
        missMc || missTrim
          ? `飞牛有、MC 没有 ${missMc} 部；MC 有、飞牛没有 ${missTrim} 部（详情见上方）`
          : "两边条目一致",
        missMc ? "bad" : "ok"
      );
      busy(e.target, false);
    } catch (err) {
      toast(err.message, "bad");
      busy(e.target, false);
    }
  });
  $("#btn-add-lib").addEventListener("click", openAddDialog);
  $("#add-lib-ok").addEventListener("click", (e) => { e.preventDefault(); submitAddLib(); });
  $("#btn-mk-load").addEventListener("click", async (e) => {
    busy(e.target, true, "读飞牛中…");
    try { await loadMarkers(true); } finally { busy(e.target, false); }
  });
  $("#mk-search").addEventListener("input", (e) => {
    MK_QUERY = (e.target.value || "").trim().toLowerCase();
    MK_PAGE.shown = MK_PAGE.size;
    renderMarkers();
  });
  $("#mk-filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-mkfilter]");
    if (!b) return;
    MK_FILTER = b.dataset.mkfilter;
    MK_PAGE.shown = MK_PAGE.size;
    renderMarkers();
  });
  $("#btn-mk-push-all").addEventListener("click", async (e) => {
    const todo = MK_ROWS.filter(mkNeedsWork).length;
    const msg = todo
      ? `把 MC 识别的片头片尾全部推给飞牛？\n\n当前有 ${todo} 季需要处理（不一致 / 未设置），值没变的季会自动跳过。`
      : "当前没有需要处理的季（全部一致或飞牛侧无 MC 识别值）。\n\n仍要重置全部跳过记录并重新推送一遍吗？";
    if (!confirm(msg)) return;
    busy(e.target, true, "推送中…");
    try {
      const r = await api("/markers/push", { method: "POST", body: "{}" });
      toast(`推送完成：写入 ${r.written}、跳过 ${r.skipped}、失败 ${r.failed}`, r.failed ? "bad" : "ok");
      loadMarkers();
    } catch (err) { toast(err.message, "bad"); }
    finally { busy(e.target, false); }
  });

  // 各区块独立加载：一个接口挂了只空它自己那一块，不拖垮整页
  const safe = (fn) => () => Promise.resolve(fn()).catch((err) => console.error("[飞牛影视]", err));
  safe(loadStatus)();
  safe(loadLibraries)();
  safe(loadFnLibraries)();
  safe(loadUnidentified)();
  safe(() => loadAccount(false))();
  safe(loadLogs)();
  safe(loadMarkers)();
  setInterval(safe(loadStatus), 30000);
  setInterval(safe(loadLogs), LOG_POLL_MS);
})();

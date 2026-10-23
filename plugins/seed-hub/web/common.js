/* 保种管家页面共用逻辑。纯函数集中在 LOGIC 里，页面与 node 自测共用。 */
(function (root) {
  "use strict";

  var LOGIC = {
    /** 报告里的数字（缺省显示 0） */
    num: function (v) {
      if (v === null || v === undefined || v === "") return 0;
      var n = Number(v);
      return isFinite(n) ? n : 0;
    },

    /** 定时任务列表 → {key: task}，只挑本插件的 */
    pickSeedHubTasks: function (tasks, prefix) {
      var out = {};
      prefix = prefix || "seed-hub.";
      (tasks || []).forEach(function (t) {
        if (t && typeof t.key === "string" && t.key.indexOf(prefix) === 0) out[t.key] = t;
      });
      return out;
    },

    /** ISO 时间 → 「今天 / 明天 / 10-21 18:00」短格式；无效返回 — */
    fmtWhen: function (iso, now) {
      if (!iso) return "—";
      var d = new Date(iso);
      if (isNaN(d.getTime())) return "—";
      now = now || new Date();
      var hh = String(d.getHours()).padStart(2, "0");
      var mm = String(d.getMinutes()).padStart(2, "0");
      var sameDay = d.toDateString() === now.toDateString();
      var tomorrow = new Date(now.getTime() + 86400000).toDateString() === d.toDateString();
      if (sameDay) return "今天 " + hh + ":" + mm;
      if (tomorrow) return "明天 " + hh + ":" + mm;
      return (d.getMonth() + 1) + "-" + d.getDate() + " " + hh + ":" + mm;
    },

    /** 间隔秒 → 人话 */
    fmtInterval: function (sec) {
      var n = LOGIC.num(sec);
      if (!n) return "—";
      if (n % 86400 === 0) return n / 86400 + " 天";
      if (n % 3600 === 0) return n / 3600 + " 小时";
      if (n % 60 === 0) return n / 60 + " 分钟";
      return n + " 秒";
    },

    /** 清理候选按原因归类（页面筛选与统计共用） */
    classifyCandidate: function (item) {
      var reason = String((item && (item.reason || item.note)) || "");
      if (item && item.kind === "warn") return "warn";
      if (reason.indexOf("tracker") >= 0 || reason.indexOf("not registered") >= 0 || reason.indexOf("banned") >= 0) return "dead";
      if (reason.indexOf("No data") >= 0 || reason.indexOf("数据") >= 0) return "nodata";
      return "other";
    },

    filterCandidates: function (items, kind) {
      var all = (items || []).slice();
      if (!kind || kind === "all") return all;
      return all.filter(function (it) { return LOGIC.classifyCandidate(it) === kind; });
    },

    /** 候选分组计数 */
    countByKind: function (items) {
      var c = { dead: 0, nodata: 0, warn: 0, other: 0 };
      (items || []).forEach(function (it) { c[LOGIC.classifyCandidate(it)] += 1; });
      return c;
    },

    /** 转种明细的结论 → 中文标签 */
    TRANSFER_VERDICTS: { transfer: "待转移", exists: "目标已有", excluded: "排除标签", missing: "数据不在", small: "体积不足" },
    verdictLabel: function (v) { return LOGIC.TRANSFER_VERDICTS[v] || "其他"; },

    /** 转种明细按结论筛选 */
    filterScanItems: function (items, verdict) {
      var all = (items || []).slice();
      if (!verdict || verdict === "all") return all;
      return all.filter(function (it) { return it && it.verdict === verdict; });
    },

    /** 转种明细按结论计数 */
    countScanVerdicts: function (items) {
      var c = { transfer: 0, exists: 0, excluded: 0, missing: 0, small: 0 };
      (items || []).forEach(function (it) { if (it && c[it.verdict] !== undefined) c[it.verdict] += 1; });
      return c;
    },

    /** 辅种台账条目按源种分组：组内按时间倒序，组间按最新时间倒序 */
    groupLedger: function (entries) {
      var groups = {};
      (entries || []).forEach(function (e) {
        if (!e || !e.target_hash) return;
        var k = e.source_hash || "?";
        if (!groups[k]) groups[k] = { source_name: e.source_name || e.source_hash || "?", entries: [], latest: 0 };
        groups[k].entries.push(e);
        groups[k].latest = Math.max(groups[k].latest, Number(e.ts) || 0);
      });
      var out = Object.keys(groups).map(function (k) { return groups[k]; });
      out.sort(function (a, b) { return b.latest - a.latest; });
      out.forEach(function (g) {
        g.entries.sort(function (a, b) { return (Number(b.ts) || 0) - (Number(a.ts) || 0); });
      });
      return out;
    },

    /** Unix 秒 → 人话时间（复用 fmtWhen） */
    fmtEpoch: function (ts) {
      var n = Number(ts);
      if (!n) return "—";
      return LOGIC.fmtWhen(new Date(n * 1000).toISOString());
    },

    /** 日志按级别过滤：level=warning 时含 error */
    filterLogs: function (logs, level) {
      var order = { info: 0, success: 0, warning: 1, error: 2 };
      var floor = order[level];
      if (floor === undefined) return (logs || []).slice();
      return (logs || []).filter(function (l) {
        return (order[l && l.level] !== undefined ? order[l.level] : 0) >= floor;
      });
    },

    /** 一条日志的级别样式名 */
    levelClass: function (level) {
      if (level === "error") return "lv-error";
      if (level === "warning") return "lv-warning";
      if (level === "success") return "lv-success";
      return "lv-info";
    },

    levelLabel: function (level) {
      return { error: "错误", warning: "警告", success: "成功", info: "信息" }[level] || "信息";
    },

    /** 转种报告 → 页面展示行 */
    transferRows: function (r) {
      if (!r) return [{ k: "状态", v: "还没有运行过" }];
      return [
        { k: "扫描（完成的种）", v: LOGIC.num(r.scanned) },
        { k: "计划转移", v: LOGIC.num(r.planned) },
        { k: "实际转移", v: LOGIC.num(r.transferred) },
        { k: "失败", v: LOGIC.num(r.failed) },
        { k: "删除源任务", v: LOGIC.num(r.deleted_source) },
        { k: "上次运行", v: r.finished_at ? LOGIC.fmtWhen(r.finished_at) : "—" }
      ];
    },

    reseedRows: function (r) {
      if (!r) return [{ k: "状态", v: "还没有运行过" }];
      return [
        { k: "扫描（完成的种）", v: LOGIC.num(r.scanned) },
        { k: "IYUU 匹配", v: LOGIC.num(r.matched) },
        { k: "计划提交", v: LOGIC.num(r.planned) },
        { k: "成功 / 已在做种", v: LOGIC.num(r.ok) + " / " + LOGIC.num(r.exists) },
        { k: "失败", v: LOGIC.num(r.failed) },
        { k: "上次运行", v: r.finished_at ? LOGIC.fmtWhen(r.finished_at) : "—" }
      ];
    },

    sweepRows: function (r) {
      if (!r) return [{ k: "状态", v: "还没有扫描过" }];
      return [
        { k: "可清理", v: LOGIC.num(r.scanned_deletable) },
        { k: "告警", v: LOGIC.num(r.warned) },
        { k: "模式", v: r.dry_run ? "只报告" : "已真删" },
        { k: "上次扫描", v: r.finished_at ? LOGIC.fmtWhen(r.finished_at) : "—" }
      ];
    },

    /** 数字带千分位 */
    fmtNum: function (n) {
      n = LOGIC.num(n);
      return n.toLocaleString ? n.toLocaleString("zh-CN") : String(n);
    },

    htmlEscape: function (s) {
      return String(s === null || s === undefined ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
  };

  if (typeof module !== "undefined" && module.exports) module.exports = LOGIC;
  root.SEEDHUB_LOGIC = LOGIC;

  /* ---------------- 页面运行时（浏览器才有） ---------------- */
  if (typeof document === "undefined") return;

  var API = "/api/v1";
  var PLUGIN = "/api/v1/plugins/seed-hub";

  function toast(msg, bad) {
    var el = document.getElementById("toast");
    if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; document.body.appendChild(el); }
    el.textContent = msg;
    el.className = "toast show" + (bad ? " bad" : "");
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.className = "toast"; }, 4200);
  }

  async function apiGet(path) {
    var r = await fetch(API + path, { credentials: "same-origin", headers: { "Accept": "application/json" } });
    if (r.status === 403) throw new Error("需要管理员权限");
    if (!r.ok) throw new Error("接口返回 " + r.status);
    var j = await r.json();
    if (j && j.success === false) throw new Error(j.message || "接口报错");
    return j && Object.prototype.hasOwnProperty.call(j, "data") ? j.data : j;
  }

  async function apiPost(path) {
    var r = await fetch(API + path, {
      method: "POST", credentials: "same-origin",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: "{}"
    });
    if (r.status === 403) throw new Error("需要管理员权限");
    var j = null;
    try { j = await r.json(); } catch (e) { /* 空响应 */ }
    if (!r.ok) {
      var msg = (j && (j.message || (j.detail && (j.detail.detail || j.detail)))) || ("HTTP " + r.status);
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return j && Object.prototype.hasOwnProperty.call(j, "data") ? j.data : j;
  }

  /** 插件自己的接口（/api/v1/plugins/<id>/...） */
  async function pluginGet(path) {
    var r = await fetch(PLUGIN + path, { credentials: "same-origin", headers: { "Accept": "application/json" } });
    if (r.status === 403) throw new Error("需要管理员权限");
    if (!r.ok) throw new Error("插件接口返回 " + r.status);
    var j = await r.json();
    return j && Object.prototype.hasOwnProperty.call(j, "data") ? j.data : j;
  }

  async function pluginPost(path) {
    var r = await fetch(PLUGIN + path, {
      method: "POST", credentials: "same-origin",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: "{}"
    });
    if (r.status === 403) throw new Error("需要管理员权限");
    var j = null;
    try { j = await r.json(); } catch (e) { /* 空响应 */ }
    if (!r.ok) {
      var msg = (j && (j.message || (j.detail && (j.detail.detail || j.detail)))) || ("HTTP " + r.status);
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return j && Object.prototype.hasOwnProperty.call(j, "data") ? j.data : j;
  }

  function renderRows(el, rows) {
    el.innerHTML = rows.map(function (r) {
      return '<div class="kv"><span class="k">' + LOGIC.htmlEscape(r.k) + '</span><span class="v">' + LOGIC.htmlEscape(r.v) + "</span></div>";
    }).join("");
  }

  function setPill(el, text, cls) {
    if (!el) return;
    el.textContent = text;
    el.className = "pill " + (cls || "");
  }

  async function loadTasks() {
    try {
      var tasks = await apiGet("/scheduled-tasks");
      return LOGIC.pickSeedHubTasks(tasks);
    } catch (e) {
      return {};
    }
  }

  async function loadLogs(limit, level) {
    return await pluginGet("/logs?limit=" + (limit || 10) + "&level=" + (level || "info"));
  }

  window.SEEDHUB = {
    LOGIC: LOGIC, toast: toast, apiGet: apiGet, apiPost: apiPost,
    pluginGet: pluginGet, pluginPost: pluginPost, renderRows: renderRows,
    setPill: setPill, loadTasks: loadTasks, loadLogs: loadLogs
  };
})(typeof window !== "undefined" ? window : globalThis);

/* 页面呈现纯函数：折叠历史、单栏任务、状态摘要，不调用网络或改动任务。 */
"use strict";
const uiLogic = typeof module !== "undefined" && module.exports ? require("./logic.js") : null;
const uiFn = (name) => uiLogic ? uiLogic[name] : ({ canCancel, canChooseTitle, candidateOptions, fmtTime, statusLabel })[name];
function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function icon(name, cls = "") {
  const paths = {
    arrow: '<path d="m6 4 4 4-4 4"/>',
    check: '<path d="m3 8 3 3 7-7"/>',
    clock: '<circle cx="8" cy="8" r="5.5"/><path d="M8 4.5V8l2 1.5"/>',
    alert: '<circle cx="8" cy="8" r="5.5"/><path d="M8 4.5v4M8 11h.01"/>',
    stop: '<rect x="4" y="4" width="8" height="8" rx="2"/>',
    tray: '<path d="M3 3.5h10l1 8H10l-1 1H7l-1-1H2l1-8Z"/><path d="M8 2v6m-2-2 2 2 2-2"/>',
  };
  return '<svg viewBox="0 0 16 16" aria-hidden="true" class="' + escapeHtml(cls) + '">' + (paths[name] || paths.clock) + '</svg>';
}
function boardGroups(data) {
  const feed = data || {};
  const active = [].concat(feed.live || [], feed.inbox || []);
  const seen = new Set();
  return {
    active: active.filter((r) => r && !seen.has(r.job_id) && seen.add(r.job_id)),
    attention: feed.attention || [], history: feed.recent || [],
  };
}
function taskTone(status) {
  return status === "succeeded" ? "ok" : status === "failed" ? "err" : status === "blocked" ? "warn" : "";
}
function taskLabel(row) {
  if (row.status === "running" && String(row.job_type || "").endsWith(":transfer") && row.phase_label) return row.phase_label;
  return uiFn("statusLabel")(row.status);
}
function taskCaption(row) {
  if (row.status === "cancelled") return "文件与续跑记录保留";
  if (row.inventory_verified) return [row.library_name, "已核验入库"].filter(Boolean).join(" · ");
  if (row.status === "succeeded") return row.message || "处理完成";
  if (row.status === "blocked" && uiFn("canChooseTitle")(row)) return "需要确认作品后继续";
  return row.message || row.library_name || "等待后台处理";
}
function titleOf(row) {
  if (row.title && row.title !== "115 链接入库") return row.title;
  return String(row.job_type || "").endsWith(":cleanup-staging") ? "空暂存目录清理" : "链接入库";
}
function candidateHtml(row) {
  const opts = uiFn("candidateOptions")(row.candidates);
  if (!opts.length) return "";
  return '<details class="candidate-section"><summary>插件推荐候选 · ' + opts.length + '</summary><div class="chips">'
    + opts.map((o) => '<button class="chip" data-retry="' + escapeHtml(row.job_id) + '" data-tmdb="'
      + o.tmdb_id + '" data-kind="' + o.kind + '">' + escapeHtml(o.label) + '</button>').join("") + '</div></details>';
}
function taskHtml(row, expanded = false) {
  const esc = escapeHtml;
  const id = esc(row.job_id);
  // DOM id 不拼入未经编码的接口数据；data 属性仍用原任务编号供接口消费。
  const detailId = "detail-" + encodeURIComponent(String(row.job_id));
  const retryable = ["failed", "blocked", "cancelled"].includes(row.status);
  const needsChoice = uiFn("canChooseTitle")(row);
  const tone = taskTone(row.status);
  const symbol = row.status === "succeeded" ? "check" : row.status === "cancelled" ? "stop"
    : ["failed", "blocked"].includes(row.status) ? "alert" : "clock";
  let html = '<article class="task"><div class="task-head"><button class="task-toggle" data-expand="' + id
    + '" aria-expanded="' + expanded + '" aria-controls="' + esc(detailId) + '">'
    + '<span class="task-icon ' + tone + '">' + icon(symbol) + '</span><span class="grow">'
    + '<span class="task-name">' + esc(titleOf(row)) + '</span><span class="task-caption">' + esc(taskCaption(row))
    + '</span></span><span class="task-state ' + tone + '"><span>' + esc(taskLabel(row))
    + '</span><span class="task-time">' + esc(uiFn("fmtTime")(row.finished_at || row.updated_at || row.created_at))
    + '</span></span>' + icon("arrow", "task-chevron") + '</button>';
  const quick = [];
  if (needsChoice && ["blocked", "failed"].includes(row.status)) quick.push('<button class="chip confirm-title" data-search-job="' + id + '">确认作品</button>');
  if (uiFn("canCancel")(row)) quick.push('<button class="chip ghost danger" data-cancel="' + id + '">取消</button>');
  if (quick.length) html += '<div class="quick-actions">' + quick.join("") + '</div>';
  html += '</div><div class="task-details" id="' + esc(detailId) + '"' + (expanded ? '' : ' hidden') + '>';
  if (row.message) html += '<div class="msg">' + esc(row.message) + '</div>';
  const percent = Number(row.percent);
  if (Number.isFinite(percent) && percent > 0 && percent < 100 && !["cancelled", "succeeded"].includes(row.status)) {
    html += '<div class="bar" role="progressbar" aria-label="入库进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'
      + Math.round(percent) + '"><i style="width:' + Math.round(percent) + '%"></i></div>';
  }
  const links = [];
  if (row.media_item_id) {
    if (row.library_id) links.push('<a href="/library/' + encodeURIComponent(row.library_id) + '/item/' + encodeURIComponent(row.media_item_id) + '" target="_top">媒体库 ↗</a>');
    links.push('<a href="/play/' + encodeURIComponent(row.media_item_id) + '" target="_top">播放</a>');
  }
  links.push('<a href="/tasks" target="_top">任务详情 ↗</a>');
  if (needsChoice) links.push('<button class="chip" data-search-job="' + id + '">搜索 TMDB</button>');
  if (row.job_type === "p115-import:transfer" && row.status === "succeeded" && row.inventory_verified && row.pending_delete_count > 0) {
    links.push('<button class="chip danger" data-cleanup-old="' + id + '">预览清理旧文件（' + Number(row.pending_delete_count) + '）</button>');
  }
  if (retryable) links.push('<button class="chip" data-retry="' + id + '">' + (row.status === 'cancelled' ? '重新继续' : '重试') + '</button>');
  if (row.status === 'failed') links.push('<button class="chip ghost" data-dismiss="' + id + '">忽略</button>');
  html += '<div class="detail-actions">' + links.join("") + '</div>';
  // 已取消的历史仅保留必要操作，不把旧候选继续铺在首页。
  if (needsChoice && row.status !== "cancelled") html += candidateHtml(row);
  return html + '</div></article>';
}
function emptyHtml(tab) {
  const copy = {
    active: ["暂无进行中的任务", "粘贴链接，开始下一次入库"],
    attention: ["没有需要处理的任务", "遇到识别问题时，会在这里提醒你"],
    history: ["还没有历史记录", "已完成或取消的任务会保留在这里"],
  }[tab] || ["暂无任务", ""];
  return '<div class="empty"><div class="empty-symbol">' + icon("check") + '</div><strong>'
    + copy[0] + '</strong><p>' + copy[1] + '</p></div>';
}
if (typeof module !== "undefined" && module.exports) module.exports = {
  escapeHtml, icon, boardGroups, taskTone, taskLabel, taskCaption, titleOf, candidateHtml, taskHtml, emptyHtml,
};

function oldCleanupHtml(value) {
  const esc = escapeHtml;
  if (!value || value.status !== "preview") return '<p>' + esc((value || {}).message || "没有可清理的旧文件") + '</p>';
  return '<p><b>' + esc(value.title) + '</b> · ' + esc(value.library_name || "") + '</p>'
    + (value.files || []).map((f) => '<section class="preview-card"><b>旧版：' + esc(f.old.name) + '</b>'
      + '<p class="small">原文件 ' + esc(f.old.size) + ' 字节；替代版本 '
      + esc((f.new || []).map(n => n.name + '（' + n.size + ' 字节）').join('、')) + '</p>'
      + '<p class="small muted">将删除（含同名附属文件）：</p><ul>'
      + (f.removed_paths || []).map(p => '<li class="small" style="overflow-wrap:anywhere">' + esc(p) + '</li>').join('')
      + '</ul></section>').join('');
}
if (typeof module !== "undefined" && module.exports) Object.assign(module.exports, { oldCleanupHtml });

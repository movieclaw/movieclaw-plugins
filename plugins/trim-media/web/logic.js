/* 飞牛影视管理页的纯函数：不碰 DOM，便于用 node 直接跑正反例。
   app.js 从这里取用；页面用 <script src="logic.js"> 引入。 */
(function (root) {
  "use strict";

  /** 时间的相对显示；非法输入返回原样。 */
  function relTime(iso) {
    if (!iso) return "从未";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return String(iso);
    const diff = Date.now() - t;
    if (diff < 0) return "刚刚";
    const m = Math.floor(diff / 60000);
    if (m < 1) return "刚刚";
    if (m < 60) return `${m} 分钟前`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} 小时前`;
    const d = Math.floor(h / 24);
    return `${d} 天前`;
  }

  /** 字节 → 人类可读。 */
  function humanBytes(n) {
    if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return "0";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
  }

  /** 库的统计概要文案。 */
  function libStatText(stats) {
    if (!stats) return "—";
    const miss = stats.missing_count ? `，缺 ${stats.missing_count}` : "";
    return `${stats.item_count ?? 0} 部 / ${stats.file_count ?? 0} 文件${miss}`;
  }

  /** 飞牛库分类 → 显示名。 */
  function categoryLabel(category) {
    return { Movie: "电影", TV: "剧集", Mix: "混合" }[category] || category || "未知";
  }

  /** 进度百分比（0-100，封顶）。 */
  function progressPct(positionMs, durationMs) {
    if (!positionMs || !durationMs || durationMs <= 0) return 0;
    return Math.max(0, Math.min(100, (positionMs / durationMs) * 100));
  }

  /** 差异检查报告的一行摘要（飞牛与 MC 的条目数、两边各多出几部）。 */
  function alignSummary(report) {
    if (!report) return "";
    if (report.error) return `检查失败：${report.error}`;
    const a = (report.missing_in_mc || []).length;
    const b = (report.missing_in_trim || []).length;
    if (!a && !b) return `飞牛 ${report.trim_entries} 部 / MC ${report.mc_entries} 部，两边一致`;
    return `飞牛 ${report.trim_entries} 部 / MC ${report.mc_entries} 部，飞牛多 ${a} 部、MC 多 ${b} 部`;
  }

  /** 片头片尾一行「需要处理吗」：不一致 / 未设置 = 待处理。 */
  function mkNeedsWork(r) {
    return Boolean(r) && (r.state === "diff" || r.state === "unset");
  }

  const api = {
    relTime, humanBytes, libStatText, categoryLabel,
    progressPct, alignSummary, mkNeedsWork,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TrimLogic = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

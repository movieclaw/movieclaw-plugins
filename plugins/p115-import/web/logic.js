/* 看板页面的纯逻辑：分类、格式化、链接与文案。
   与 p115board.py 的口径一致（宿主任务字段就是那些）。
   浏览器里用 <script src="logic.js"> 引入；node 下 module.exports 便于正反例自测。 */
"use strict";

const PHASE_LABELS = {
  recognize: "识别与库存检查",
  dispatch: "已交接，排队转存",
  prepare: "准备暂存目录",
  submit: "提交网盘转存",
  cloud: "等待网盘放行",
  organize: "按模板归位",
  verify: "库存核验",
  index: "媒体库登记",
  cleanup: "清理暂存",
  queued: "排队中",
  completed: "已完成",
};

const STATUS_LABELS = {
  queued: "排队中",
  running: "进行中",
  retry_wait: "稍后重试",
  waiting: "等待中",
  cancelling: "正在取消",
  succeeded: "已完成",
  failed: "失败",
  blocked: "需要处理",
  cancelled: "已取消",
};

const ACTIVE = ["queued", "running", "retry_wait", "waiting", "cancelling"];

function isActive(job) {
  return ACTIVE.indexOf(String((job || {}).status || "")) >= 0;
}

function phaseLabel(phase) {
  const key = String(phase || "");
  return PHASE_LABELS[key] || key || "";
}

function statusLabel(status) {
  return STATUS_LABELS[String(status || "")] || String(status || "");
}

/* ---- 链接判断：与插件入口 split_inputs / parse_offline 的接受范围一致 ---- */
const SHARE = /https?:\/\/(?:[a-z0-9-]+\.)?115(?:cdn)?\.com\/s\/[^\s，。；、<>）)\]】]+/i;
const ED2K = /ed2k:\/\/\|file\|[^|]+\|\d+\|[a-f0-9]{32}\|?/i;
const MAGNET = /magnet:\?[^\s，。；、<>）)\]】]+/i;

function classifyInput(text) {
  const value = String(text || "");
  if (!value.trim()) return "empty";
  if (!SHARE.test(value) && !ED2K.test(value) && !MAGNET.test(value)) return "invalid";
  return "link";
}

/* ---- 格式化 ---- */
function fmtSize(bytes) {
  let size = Number(bytes || 0);
  if (!size) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return index === 0 ? `${size} B` : `${size.toFixed(1)} ${units[index]}`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (isNaN(date.getTime())) return "";
  const now = Date.now();
  const diff = Math.round((now - date.getTime()) / 1000);
  if (diff >= 0 && diff < 60) return "刚刚";
  if (diff >= 0 && diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff >= 0 && diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  const pad = (n) => String(n).padStart(2, "0");
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  const head = `${sameYear ? "" : date.getFullYear() + "-"}${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return `${head} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const TERMINAL = ["succeeded", "failed", "blocked", "cancelled"];

/* 只要任务还没到终点，看板就继续轮询 */
function needsPoll(feed) {
  const data = feed || {};
  return ["inbox", "live"].some((key) =>
    (data[key] || []).some((row) => !TERMINAL.includes(String(row.status || "")))
  );
}

const JUMP = {
  tasks: "/tasks",
  library: (row) => (row && row.media_item_id ? `/library/${row.library_id || ""}` : "/library"),
};

function entryLinks(row) {
  const links = [];
  if (row && row.media_item_id) {
    const lib = row.library_id ? `/library/${row.library_id}/item/${row.media_item_id}` : "";
    if (lib) links.push({ label: "去媒体库看", href: lib, top: true });
    links.push({ label: "播放", href: `/play/${row.media_item_id}`, top: true });
  }
  links.push({ label: "任务页", href: JUMP.tasks, top: true });
  return links;
}

/* 提交框的行为判定：禁用与否、预演/提交按钮的文案 */
function submitState(text, preview) {
  const kind = classifyInput(text);
  return {
    kind,
    disabled: kind !== "link",
    hint: kind === "empty" ? "" : kind === "invalid"
      ? "只认 115 分享链接 / 电驴 / 磁力链接，多条可分行粘贴" : "",
    action: preview ? "preview" : "apply",
  };
}

/* candidates → 确认控件描述（识别有歧义时让用户直接选） */
function candidateOptions(candidates) {
  return (candidates || []).filter((row) => row && typeof row === "object").map((row) => ({
    tmdb_id: Number(row.tmdb_id) || 0,
    kind: row.kind === "tv" ? "tv" : "movie",
    label: `${row.title || "（未命名）"}${row.year ? `（${row.year}）` : ""}`,
  })).filter((row) => row.tmdb_id > 0);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    PHASE_LABELS, STATUS_LABELS, ACTIVE,
    isActive, phaseLabel, statusLabel, classifyInput,
    fmtSize, fmtTime, needsPoll, entryLinks, submitState, candidateOptions,
  };
}

/* 取消仅限本插件；已取消记录保留在最近记录，不自动续跑。 */
function canCancel(row) {
  return !!(row && row.job_id && String(row.job_type || "").startsWith("p115-import:")
    && ["queued", "running", "retry_wait", "waiting", "blocked"].includes(row.status));
}
function canChooseTitle(row) {
  return !!(row && row.job_type === "p115-import:request"
    && ["failed", "blocked", "cancelled"].includes(row.status));
}
function tmdbSearchOptions(titles, kind) {
  return (titles || []).filter((row) => row && row.provider === "tmdb"
    && ["tv", "movie"].includes(row.media_type) && (!kind || row.media_type === kind)
    && /^\d+$/.test(String(row.external_id || "")) && Number(row.external_id) > 0)
    .map((row) => ({ tmdb_id: Number(row.external_id), kind: row.media_type,
      title: row.title || row.original_title || "（未命名）", year: row.release_year || null,
      original_title: row.original_title || "", overview: row.overview || "" }));
}
if (typeof module !== "undefined" && module.exports) {
  Object.assign(module.exports, { canCancel, canChooseTitle, tmdbSearchOptions });
}

/* 页面不可见时不轮询；连续错误逐步退避，避免反复打接口。 */
function boardPollDelay(feed, hidden, failures = 0) {
  if (hidden) return null;
  if (failures > 0) return Math.min(120000, 5000 * Math.pow(2, Math.min(failures - 1, 5)));
  return needsPoll(feed) ? 4000 : 30000;
}
function canConfirmOldCleanup(value) {
  return !!(value && value.status === "preview" && value.approval_token && value.files && value.files.length
    && Number(value.expires_at) * 1000 > Date.now()
    && value.files.every(f => f.old && Number.isInteger(f.old.file_id) && f.old.file_id > 0
      && Array.isArray(f.removed_paths) && f.removed_paths.length > 0));
}
if (typeof module !== "undefined" && module.exports) Object.assign(module.exports, { boardPollDelay, canConfirmOldCleanup });

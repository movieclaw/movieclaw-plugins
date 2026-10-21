"use strict";

// 片源状态只在当前浏览器身份下读取，绝不使用好友服务器的库存编号或插件管理身份。
// 确认作品身份后才投影本服入口；查询结果不写入片单、动态或任何跨服数据。
window.MovieFriendsAvailability = (() => {
  let bridge;
  let epoch = 0;
  const cache = new Map();
  const jobs = new WeakMap();
  const observer = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target);
      jobs.get(entry.target)?.();
    }
  }, { rootMargin: "100px" }) : null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function keyFor(item) {
    const id = item?.tmdb_id;
    return Number.isSafeInteger(id) && id > 0 && ["movie", "tv"].includes(item.kind) ? `tmdb:${item.kind}:${id}` : null;
  }

  function link(label, href, primary = false) {
    const element = node("a", `button ${primary ? "button-primary" : "button-quiet"}`, label);
    element.href = href; element.target = "_top";
    return element;
  }

  function project(result, item) {
    const title = result?.title;
    const key = keyFor(item);
    const exact = title && (title.title_ref === key || (title.provider === "tmdb" && Number(title.external_id) === item.tmdb_id && title.media_type === item.kind));
    if (!exact || (title.title_ref && title.title_ref !== key) || (title.external_id != null && Number(title.external_id) !== item.tmdb_id) || (title.provider && title.provider !== "tmdb") || (title.media_type && title.media_type !== item.kind)) throw new Error("影片身份无法核对");
    const status = title.library_status;
    const links = Array.isArray(result.library_links) ? result.library_links : null;
    if (!Object.hasOwn(title, "library_status") || !links) throw new Error("宿主暂未提供库存信息");
    if (status === null && links.length === 0) return { available: false, links: [] };
    if (!status || !Number.isSafeInteger(status.media_item_id) || status.media_item_id <= 0 || !Number.isSafeInteger(status.file_count) || status.file_count < 0) throw new Error("库存信息格式不兼容");
    const visible = links.filter((entry) => Number.isSafeInteger(entry.library_id) && entry.library_id > 0 && entry.media_item_id === status.media_item_id).slice(0, 8);
    if (status.file_count > 0 && visible.length) return { available: true, itemId: status.media_item_id, links: visible.map((entry) => ({ libraryId: entry.library_id, itemId: entry.media_item_id })) };
    if (status.file_count === 0 && !links.length) return { available: false, links: [] };
    throw new Error("片源入口暂不可用");
  }

  function lookup(item) {
    const key = keyFor(item);
    const session = bridge.sessionVersion();
    const existing = cache.get(key);
    if (existing && existing.session === session && existing.expires > Date.now()) return existing.pending;
    const pending = bridge.hostApi(`/discover/titles/${encodeURIComponent(key)}`).then((result) => project(result, item));
    const entry = { session, pending, expires: Date.now() + 30_000 };
    cache.set(key, entry);
    pending.catch(() => { if (cache.get(key) === entry) cache.delete(key); });
    return pending;
  }

  function create(record) {
    const item = record?.media || record || {};
    const widget = node("section", "availability-widget");
    widget.setAttribute("aria-label", "当前账号可见的本服片源");
    const status = node("p", "availability-status muted", "正在检查本服片源…");
    const actions = node("div", "availability-actions");
    widget.append(status, actions);
    const version = epoch;
    const session = bridge?.sessionVersion();
    const key = keyFor(item);
    const query = encodeURIComponent(String(item.title || "").slice(0, 100));
    const sourceSearch = () => link("搜索资源", `/search?q=${query}&private=1`);
    const active = () => bridge?.user() && epoch === version && session === bridge.sessionVersion() && widget.isConnected;

    async function check() {
      if (!active()) return;
      if (!key) {
        widget.dataset.state = "unknown";
        status.textContent = "缺少可靠作品编号，暂时无法确认片源";
        actions.replaceChildren(link("搜索影片", `/search?q=${query}&tab=media&private=1`));
        return;
      }
      status.textContent = "正在检查本服片源…";
      try {
        const result = await lookup(item);
        if (!active()) return;
        widget.dataset.state = result.available ? "available" : "missing";
        status.textContent = result.available ? "当前账号有可访问的本服片源" : "当前账号暂未找到本服片源";
        if (result.available) {
          const first = result.links[0];
          const library = `/library/${first.libraryId}/item/${first.itemId}`;
          actions.replaceChildren(link(item.kind === "movie" ? "立即观看" : "选集观看", item.kind === "movie" ? `/play/${result.itemId}` : library, true), link("查看本服片源", library), sourceSearch());
        } else actions.replaceChildren(sourceSearch(), link("查看影片详情", `/media/${item.kind}/${item.tmdb_id}`));
      } catch (error) {
        if (!active() || error.staleSession) return;
        widget.dataset.state = "unknown";
        status.textContent = error.status === 403 ? "当前账号无权查询片源状态" : "片源状态暂时无法确认";
        const retry = node("button", "button button-quiet", "重新检查");
        retry.type = "button";
        retry.addEventListener("click", () => { cache.delete(key); check(); });
        actions.replaceChildren(retry, sourceSearch());
      }
    }

    jobs.set(widget, check);
    if (observer) observer.observe(widget); else queueMicrotask(check);
    return widget;
  }

  return {
    configure(value) { bridge = value; }, create,
    reset() { epoch += 1; cache.clear(); observer?.disconnect(); },
  };
})();

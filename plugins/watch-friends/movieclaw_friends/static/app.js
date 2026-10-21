"use strict";

// 原生插件网页使用宿主当前登录身份，同源请求自动携带宿主会话。
// 不接收或透传 MovieClaw 令牌；动态内容统一通过 textContent 写入。
(() => {
  const API_BASE = "/api/v1/plugins/watch-friends";
  const DEMO_PREVIEW_ID = "__demo_preview__:";
  const byId = (id) => document.getElementById(id);
  const state = { user: null, tab: "profile", friends: [], incoming: [], outgoing: [], directory: [], directoryVersion: 0, collections: [], browserCollections: false, demo: null, demoVersion: 0, demoSaving: false, lists: [], listDraft: [], listCover: null, favorites: [], publicFavorites: [], favoriteDraft: [], editorMode: "list", editorVersion: 0, listSearchVersion: 0, friendId: null, detailVersion: 0, mediaVersion: 0, sessionVersion: 0, refreshing: null, pendingPreview: null };
  const titleCache = new Map();
  let toastTimer;
  let consentResolve;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function button(label, className, handler) {
    const element = node("button", `button ${className}`);
    window.MovieFriendsIcons.label(element,label);
    element.type = "button";
    element.addEventListener("click", () => runAction(element, handler));
    return element;
  }

  function safeError(error) {
    // 上游连接错误有时包含请求地址，错误提示也不能意外展示服务器地址。
    return String(error?.message || "操作未完成，请稍后重试。")
      .replace(/https?:\/\/[^\s<>"'，。；）)]+/gi, "[连接地址已隐藏]");
  }

  function toast(message, isError = false) {
    clearTimeout(toastTimer);
    const element = byId("toast");
    element.textContent = message;
    element.classList.toggle("is-error", isError);
    element.hidden = false;
    toastTimer = setTimeout(() => { element.hidden = true; }, isError ? 7000 : 4000);
  }

  async function runAction(element, callback) {
    if (element.disabled) return;
    const version = state.sessionVersion;
    element.disabled = true;
    element.setAttribute("aria-busy", "true");
    try { await callback(); } catch (error) { if (version === state.sessionVersion && !error.staleSession) toast(safeError(error), true); }
    finally { element.disabled = false; element.removeAttribute("aria-busy"); }
  }

  function requireSession(version) {
    if (version === state.sessionVersion) return;
    const error = new Error("该操作所属的登录会话已结束。");
    error.staleSession = true;
    throw error;
  }

  const api = (path, options) => requestJson(`${API_BASE}${path}`, options);
  const hostApi = (path, options) => requestJson(`/api/v1${path}`, options);

  async function requestJson(path, options = {}) {
    const version = state.sessionVersion;
    const method = options.method || "GET";
    const headers = { Accept: "application/json" };
    if (method !== "GET" && path.startsWith(`${API_BASE}/`)) headers["X-Friends-Request"] = "1";
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    } catch { requireSession(version); throw new Error("暂时无法连接映迹，请检查网络后重试。"); }
    let data;
    let invalidJson = false;
    try { data = await response.json(); } catch { data = {}; invalidJson = true; }
    // 请求返回时可能已经退出或切换账号，旧 401 也不能注销后来登录的会话。
    requireSession(version);
    if (response.ok && (invalidJson || !data || typeof data !== "object")) throw new Error("主程序未返回有效数据，请稍后重试。");
    if (!data || typeof data !== "object") data = {};
    if (!response.ok || data.success === false) {
      if (response.status === 401) showLoginRequired();
      const detail = typeof data.detail === "string" ? data.detail : typeof data.message === "string" ? data.message : null;
      const error = new Error(detail || (response.status === 401 ? "主站登录已失效，请返回 MovieClaw 登录。" : response.status === 403 ? "当前账号没有此项权限，请联系管理员。" : "操作未完成，请稍后重试。"));
      error.status = response.status;
      throw error;
    }
    return Object.hasOwn(data, "success") && Object.hasOwn(data, "data") ? data.data : data;
  }

  const list = (value) => Array.isArray(value) ? value : [];
  const sameId = (left, right) => String(left) === String(right);
  const displayName = (user) => user?.nickname || user?.username || "观影同好";
  const media = (record) => record?.media || record || {};
  const title = (record) => media(record).title || record?.title || "未命名影片";

  function dateText(value, includeTime = true) {
    if (!value) return "时间未提供";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "时间未提供";
    return new Intl.DateTimeFormat("zh-CN", { ...(date.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}), month: "numeric", day: "numeric", ...(includeTime ? { hour: "2-digit", minute: "2-digit" } : {}) }).format(date);
  }

  function durationText(value) {
    if (value === null || value === undefined) return "--:--";
    const milliseconds = Number(value);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "--:--";
    const seconds = Math.floor(milliseconds / 1000);
    const minutes = Math.floor(seconds / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}` : `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function mediaMeta(record) {
    const item = media(record);
    const values = [];
    if (item.year) values.push(item.year);
    const season = item.season_number ?? record?.season;
    const episode = item.episode_number ?? record?.episode;
    if (season !== undefined && season !== null) values.push(`第 ${season} 季`);
    if (episode !== undefined && episode !== null) values.push(`第 ${episode} 集`);
    // 部分服务器把「第47集 / 第四十七集 / Episode 47」当作集名，避免与季集号重复。
    const episodeTitle = String(item.episode_title || "").trim();
    const genericTitle = /^(?:第?[0-9零〇一二两三四五六七八九十百千]+[集话話]|(?:episode|ep\.?|e)\d+|s\d+e\d+)$/i.test(episodeTitle.replace(/\s+/g, ""));
    if (episodeTitle && !(episode != null && genericTitle)) values.push(episodeTitle);
    return values.join(" · ");
  }

  function posterPath(item) {
    const pattern = /^\/[A-Za-z0-9_-]{1,200}\.(jpg|jpeg|png|webp)$/i;
    if (typeof item?.poster_path === "string" && pattern.test(item.poster_path)) return item.poster_path;
    // 宿主本人片单只提供 poster_url；只从固定 TMDB 图片域提取单文件名。
    const match = typeof item?.poster_url === "string" && item.poster_url.match(/^https:\/\/image\.tmdb\.org\/t\/p\/(?:w[1-9]\d*|original)(\/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg|png|webp))$/i);
    return match ? match[1] : null;
  }

  function titleKey(item) {
    const id = Number(item?.tmdb_id);
    const kind = item?.kind === "movie" ? "movie" : ["tv", "show", "season", "episode"].includes(item?.kind) ? "tv" : null;
    return kind && Number.isSafeInteger(id) && id > 0 ? `tmdb:${kind}:${id}` : null;
  }

  function backdropPath(item) {
    return posterPath({ poster_path: item?.backdrop_path, poster_url: item?.backdrop_url });
  }

  function titleDetails(item) {
    const key = titleKey(item);
    if (!key) return Promise.resolve(null);
    if (!titleCache.has(key)) {
      const pending = hostApi(`/discover/titles/${encodeURIComponent(key)}`).then((result) => {
        // 发现详情使用 title/metadata 两层结构，只投影公开作品资料，丢弃本地库存与文件入口。
        const source = result?.title;
        const metadata = result?.metadata || {};
        const [, kind, id] = key.split(":");
        const validIdentity = source && typeof source === "object"
          && (!source.title_ref || source.title_ref === key)
          && (source.external_id == null || Number(source.external_id) === Number(id))
          && (!source.media_type || source.media_type === kind)
          && (!source.provider || source.provider === "tmdb")
          && (source.title_ref === key || (Number(source.external_id) === Number(id) && source.media_type === kind && source.provider === "tmdb"));
        if (!validIdentity) throw new Error("主程序返回的影片身份不匹配，已停止展示该详情。");
        const text = (value) => typeof value === "string" ? value : "";
        // 图片只投影 TMDB 单文件名，不把库存、来源 URL 或本地路径交给推荐编辑器。
        const images=(items,type)=>list(items).map(image=>({path:posterPath({poster_url:image?.full_url})||posterPath({poster_url:image?.preview_url}),type})).filter(image=>image.path);
        return {
          images:[...images(result.backdrops,"backdrop"),...images(result.posters,"poster")],
          title: text(source.title), overview: text(source.overview),
          poster_path: posterPath(source), backdrop_path: backdropPath(source), year: Number.isInteger(source.release_year) ? source.release_year : null,
          genres: list(source.genres).filter((genre) => typeof genre === "string"),
          extent_label: text(source.extent_label),
          runtime_minutes: Number(source.runtime_minutes) > 0 ? Number(source.runtime_minutes) : null,
          directors: list(metadata.directors).filter((director) => typeof director === "string"),
          country: text(metadata.country), language: text(metadata.language), released: text(metadata.released),
        };
      });
      titleCache.set(key, pending);
      pending.catch(() => { if (titleCache.get(key) === pending) titleCache.delete(key); });
    }
    return titleCache.get(key);
  }

  async function fullArtwork(item) {
    const key=titleKey(item);if(!key)throw new Error("缺少可靠影片身份，无法读取图库。");
    const result=await hostApi(`/discover/titles/${encodeURIComponent(key)}`),title=result?.title;
    if(!title||title.title_ref!==key||(title.external_id!=null&&Number(title.external_id)!==item.tmdb_id)||(title.media_type&&title.media_type!==item.kind))throw new Error("图库影片身份不匹配。");
    const status=title.library_status,links=list(result.library_links),first=links.find(row=>Number.isSafeInteger(row.library_id)&&row.library_id>0&&Number.isSafeInteger(row.media_item_id)&&row.media_item_id>0&&row.media_item_id===status?.media_item_id);
    if(!first)throw new Error("此影片尚无当前账号可访问的本服片源，暂时仅能使用主程序详情提供的图片。");
    const candidates=await hostApi(`/libraries/${first.library_id}/items/${first.media_item_id}/artwork/candidates`);
    const project=(values,type)=>list(values).filter(v=>!v.source||v.source==="tmdb").map(v=>({path:posterPath({poster_path:v.file_path})||posterPath({poster_url:v.preview_url}),type})).filter(v=>v.path);
    return {images:[...project(candidates.backdrops,"backdrop"),...project(candidates.posters,"poster")],notice:"已读取主程序可用的完整 TMDB 候选图库；语言和来源范围沿用主程序配置。"};
  }

  function mediaPoster(record, isDemo = false, interactive = true, width = 240) {
    const item = media(record);
    const poster = node(interactive ? "button" : "div", "media-poster");
    poster.setAttribute("aria-label", `${title(record)}${interactive ? "，查看影片详情" : "，海报"}`);
    if (interactive) {
      poster.type = "button";
      poster.addEventListener("click", () => showMedia(record, isDemo));
    }
    const fallback = node("span", "poster-fallback");
    fallback.append(window.MovieFriendsIcons.create("image"), node("span", "", "暂无海报"));
    fallback.setAttribute("aria-hidden", "true");
    poster.append(fallback);
    function setImage(source) {
      const image = node("img");
      image.alt = `${title(record)}海报`;
      image.loading = "lazy";
      image.referrerPolicy = "same-origin";
      image.addEventListener("error", () => image.remove());
      image.src = source;
      poster.append(image);
    }
    {
      const path = posterPath(item);
      if (path) setImage(`/api/v1/images/proxy?url=${encodeURIComponent(`https://image.tmdb.org/t/p/${width>240?"original":"w500"}${path}`)}&w=${width}`);
      else if (titleKey(item)) {
        const version = state.sessionVersion;
        titleDetails(item).then((details) => {
          if (version !== state.sessionVersion || !poster.isConnected) return;
          const found = posterPath(details);
          if (found) setImage(`/api/v1/images/proxy?url=${encodeURIComponent(`https://image.tmdb.org/t/p/${width>240?"original":"w500"}${found}`)}&w=${width}`);
        }).catch(() => {});
      }
    }
    return poster;
  }

  function mediaTitle(record, isDemo = false) {
    const titleButton = node("button", "media-title", title(record));
    titleButton.type = "button";
    titleButton.addEventListener("click", () => showMedia(record, isDemo));
    return titleButton;
  }

  // 横版画面只取当前账号按 TMDB 身份核对的公开剧照；无剧照时居中展示原海报，不裁成假剧照。
  // 与片单封面共用可见区域观察器，离屏作品不提前查询；请求不写入推荐或分享快照。
  function mediaArtwork(record, isDemo = false, interactive = true, width = 240) {
    const item = media(record);
    const artwork = node(interactive ? "button" : "div", "media-artwork");
    if (interactive) {
      artwork.type = "button";
      artwork.setAttribute("aria-label", `${title(record)}，查看影片详情`);
      artwork.addEventListener("click", () => showMedia(record, isDemo));
    }
    const placeholder = node("span", "artwork-placeholder");
    placeholder.append(mediaPoster(record, isDemo, false, width));
    artwork.append(placeholder);
    const version = state.sessionVersion;
    artwork.loadCover = async () => {
      try {
        const chosen=posterPath({poster_path:item.cover_path}),chosenType=item.cover_type;
        const explicit=chosen&&["backdrop","poster"].includes(chosenType);
        const details = explicit?null:await titleDetails(item);
        if (!state.user || version !== state.sessionVersion || !artwork.isConnected) return;
        const path = explicit?chosen:backdropPath(details);
        if (!path) return;
        const image = node("img", "artwork-backdrop");
        image.alt = `${title(record)}${explicit?"推荐封面":"剧照"}`; image.decoding = "async";
        if(explicit&&chosenType==="poster")artwork.classList.add("is-poster-cover");
        image.referrerPolicy = "same-origin";
        image.addEventListener("load", () => {
          if (version === state.sessionVersion && artwork.isConnected) artwork.classList.add("has-backdrop");
        });
        image.addEventListener("error", () => image.remove());
        image.src = `/api/v1/images/proxy?url=${encodeURIComponent(`https://image.tmdb.org/t/p/w1280${path}`)}&w=1280`;
        artwork.append(image);
      } catch { /* 剧照不可用时保留海报，不影响详情与社交操作。 */ }
    };
    coverObserver.observe(artwork);
    return artwork;
  }

  function releaseArtwork(root) {
    for (const artwork of root?.querySelectorAll(".media-artwork") || []) coverObserver.unobserve(artwork);
  }

  function historyShelf(records, isDemo = false) {
    const shelf = node("div", "profile-history-shelf");
    for (const record of list(records)) {
      const card = node("article", "profile-watch-card");
      const picture = node("div", "profile-watch-picture");
      picture.append(mediaArtwork(record, isDemo));
      const watched = record.ended_at || record.started_at;
      if (watched) picture.append(node("span", "watch-card-date", dateText(watched)));
      card.append(picture, mediaTitle(record, isDemo));
      const meta = mediaMeta(record);
      if (meta) card.append(node("small", "muted", meta));
      shelf.append(card);
    }
    return shelf;
  }

  function renderMediaDetails(record, details, isDemo, error = "") {
    const item = media(record);
    const content = node("div", "media-dialog-body");
    const copy = node("div", "media-dialog-copy");
    const heading = node("h3", "", details?.title || title(record));
    if (isDemo) heading.append(node("span", "demo-badge", "演示观看"));
    const extent = details?.extent_label || (details?.runtime_minutes ? `${details.runtime_minutes} 分钟` : "");
    copy.append(heading, node("p", "media-facts", [mediaMeta({ ...item, year: details?.year || item.year }), list(details?.genres).filter((genre) => typeof genre === "string").join(" / "), extent].filter(Boolean).join(" · ")));
    const credits = [list(details?.directors).length ? `导演：${details.directors.join(" / ")}` : "", details?.country, details?.language, details?.released].filter(Boolean);
    if (credits.length) copy.append(node("p", "media-facts", credits.join(" · ")));
    copy.append(node("p", "media-overview", details?.overview || "暂无剧情简介。"));
    if (isDemo) copy.append(node("p", "field-help", "影片为真实作品，仅用户身份、在线状态与观看行为是演示。"));
    if (titleKey(item)) {
      const link = window.MovieFriendsIcons.label(node("a", "button button-outline"), "在 MovieClaw 打开 ↗");
      link.href = `/media/${titleKey(item).split(":").slice(1).join("/")}`;
      link.target = "_top";
      copy.append(link);
    }
    copy.append(button("加入片单", "button-outline", () => addToList(item)));
    copy.append(button("加入我喜欢的电影", "button-outline", () => openFavoritesEditor(item)));
    copy.append(button("推荐到影友动态", "button-primary", () => {
      if (byId("media-dialog").open) byId("media-dialog").close();
      switchTab("feed");
      window.MovieFriendsFeed?.recommend(item);
    }));
    if (titleKey(item)) {
      const source = window.MovieFriendsIcons.label(node("a", "media-source"), "影片资料与海报：TMDB ↗");
      source.href = `https://www.themoviedb.org/${titleKey(item).split(":").slice(1).join("/")}`;
      source.target = "_blank";
      source.rel = "noopener noreferrer";
      copy.append(source);
    }
    if (error) copy.append(node("p", "empty-inline", error));
    const availability = window.MovieFriendsAvailability?.create(item);
    if (availability) copy.append(availability);
    content.append(mediaPoster({ ...item, ...(details ? { poster_path: posterPath(details) || posterPath(item) } : {}) }, isDemo, false), copy);
    byId("media-dialog-content").replaceChildren(content);
  }

  async function showMedia(record, isDemo = false) {
    const version = state.sessionVersion;
    const requestVersion = ++state.mediaVersion;
    const item = media(record);
    const demoDetails = isDemo ? state.demo?.details?.[item.demo_id] : null;
    byId("media-dialog").dataset.demo = String(isDemo);
    renderMediaDetails(record, demoDetails, isDemo);
    if (!byId("media-dialog").open) window.MovieFriendsUI.open(byId("media-dialog"));
    if ((isDemo && demoDetails) || !titleKey(item)) return;
    byId("media-dialog-content").append(node("p", "busy-text", "正在读取影片详情…"));
    try {
      const details = await titleDetails(item);
      if (!state.user || version !== state.sessionVersion || requestVersion !== state.mediaVersion || !byId("media-dialog").open) return;
      renderMediaDetails(record, details, isDemo);
    } catch (error) {
      if (!state.user || version !== state.sessionVersion || requestVersion !== state.mediaVersion || !byId("media-dialog").open) return;
      renderMediaDetails(record, null, isDemo, `详情暂时不可用。${safeError(error)}`);
    }
  }

  function mediaItems(items, isDemo = false) {
    const contents = node("ul", "media-items");
    for (const item of list(items)) {
      const row = node("li", "media-item");
      const copy = node("div", "person-copy");
      const heading = node("h4"); heading.append(mediaTitle(item, isDemo));
      copy.append(heading, node("p", "media-meta", mediaMeta(item)));
      row.append(mediaPoster(item, isDemo), copy);
      contents.append(row);
    }
    if (!list(items).length) contents.append(node("li", "empty-inline", "片单暂无条目。"));
    return contents;
  }

  function movieShelf(items, isDemo = false) {
    const shelf = node("div", "favorite-shelf");
    for (const item of list(items)) {
      const tile = node("article", "favorite-tile");
      tile.append(mediaPoster(item, isDemo), mediaTitle(item, isDemo));
      shelf.append(tile);
    }
    return shelf;
  }

  async function loadFavorites() {
    const version = state.sessionVersion;
    const result = await api("/favorites");
    requireSession(version);
    state.favorites = list(result.items);
    state.publicFavorites = list(result.public_items);
    renderFavorites();
    renderPrivacySummary();
  }

  function renderFavorites() {
    byId("favorites-count").textContent = state.favorites.length;
    byId("favorites-list").replaceChildren(state.favorites.length ? movieShelf(state.favorites) : emptyState("喜欢的电影，留在这里", "从搜索或影片详情中挑选，先为自己收藏。", "heart"));
    const published = state.user?.discoverable && state.user.share_favorites;
    byId("favorites-public-notice").textContent = published ? `私人列表可随时修改；广场仍展示上次确认的 ${state.publicFavorites.length} 部影片。重新预览并发布后才会更新。` : "当前仅自己可见，保存喜欢不会自动公开。";
    window.MovieFriendsIcons.label(byId("favorites-publish"), published ? "重新预览并确认公开 ↗" : "去设置选择公开 ↗");
  }

  async function openFavoritesEditor(item) {
    const version = state.sessionVersion;
    const editorVersion = ++state.editorVersion;
    await loadFavorites();
    requireSession(version);
    if (editorVersion !== state.editorVersion) return;
    state.favoriteDraft = state.favorites.map((entry) => ({ ...entry }));
    state.editorMode = "favorites";
    if (item) addDraftItem(media(item));
    configureEditor();
    if (byId("media-dialog").open) byId("media-dialog").close();
    if (!byId("list-create-dialog").open) window.MovieFriendsUI.open(byId("list-create-dialog"));
  }

  async function addToList(record) {
    const version = state.sessionVersion;
    const targetVersion = state.listTargetVersion = (state.listTargetVersion || 0) + 1;
    const item = portableMedia(media(record));
    const dialog = byId("list-target-dialog");
    const target = byId("list-target-options");
    target.replaceChildren(node("p", "busy-text", "正在读取你的片单…"));
    if (!dialog.open) window.MovieFriendsUI.open(dialog);
    const result = await api("/lists");
    requireSession(version);
    if (!dialog.open || targetVersion !== state.listTargetVersion) return;
    state.lists = list(result.lists);
    target.replaceChildren(...list(result.lists).map((collection) => button("", "list-target-option button-outline", async () => {
      const activeVersion = state.sessionVersion;
      const updated = await api(`/lists/${encodeURIComponent(collection.id)}/items`, { method: "POST", body: { media: item } });
      requireSession(activeVersion);
      state.lists = state.lists.map((entry) => sameId(entry.id, updated.id) ? updated : entry);
      renderLists();
      if (targetVersion === state.listTargetVersion && dialog.open) dialog.close();
      toast(collection.shared ? "已加入私人片单；好友仍看到上次的共享快照，重新分享后才会更新。" : `已加入「${collection.name}」。`);
    })));
    [...target.children].forEach((choice, index) => {
      const collection = result.lists[index];
      choice.append(node("strong", "", collection.name), node("small", "", collection.shared ? "已有共享快照 · 新增仅私人保存" : "私人片单"));
    });
    target.append(button("＋ 新建片单并加入", "button-primary", () => {
      if (state.editingList) state.listDraft = [];
      state.editingList = null;
      state.editorMode = "list";
      addDraftItem(item);
      dialog.close();
      if (byId("media-dialog").open) byId("media-dialog").close();
      openListCreator();
    }));
  }

  const coverObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) {
      coverObserver.unobserve(entry.target);
      entry.target.loadCover?.();
    }
  }, { rootMargin: "160px" });

  function collectionCover(collection, isDemo = false, nativeSource = false) {
    const cover = node("div", "cover-mosaic");
    function fill(items) {
      cover.replaceChildren();
      cover.dataset.count = String(Math.min(4, list(items).length));
      if(collection.cover){
        cover.classList.add("has-custom-cover");
        const artwork=mediaArtwork(collection.cover,isDemo,false,1280);cover.append(artwork);
      }else if(list(items).length===1){cover.append(mediaArtwork(items[0],isDemo,false,1280));}
      else for (const item of list(items).slice(0, 4)) cover.append(mediaPoster(item, isDemo, true, 500));
      if (!list(items).length && !collection.cover) {
        const empty = node("span", "cover-empty");
        empty.append(window.MovieFriendsIcons.create("list-video"), node("small", "", "为喜欢的故事留个位置"));
        cover.append(empty);
      }
    }
    fill(collection.items);
    if (nativeSource && !list(collection.items).length && collection.source_available !== false) {
      const version = state.sessionVersion;
      cover.loadCover = async () => {
        try {
          const items = await hostApi(`/collections/${encodeURIComponent(collection.id)}/items?limit=4&offset=0`);
          if (state.user && version === state.sessionVersion && cover.isConnected) fill(items);
        } catch { /* 封面加载失败不影响片单预览、分享和撤回。 */ }
      };
      coverObserver.observe(cover);
    }
    return cover;
  }

  function avatar(user, amber = false) {
    const element = node("span", `avatar${amber ? " avatar-amber" : ""}`);
    const demo = user?.is_demo ? demoUsers().find((entry) => sameId(entry.user.id, user.user_id || user.id)) : null;
    const id = user?.avatar_id || demo?.user.avatar_id;
    const source = user?.avatar_url;
    const inline = user?.avatar_data;
    if ((typeof source === "string" && /^\/api\/v1\/plugins\/watch-friends\/watch\/avatar\/[A-Za-z0-9-]+(?:\?v=[a-f0-9]+)?$/.test(source)) || (typeof inline === "string" && inline.length < 90000 && /^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(inline))) {
      const picture = node("img"); picture.src = source || inline; picture.alt = ""; element.append(picture);
    } else if (["xiaoying", "achuan", "xiaolu", "xingzi"].includes(id)) {
      const picture = node("img");
      picture.src = `${API_BASE}/web/avatars/${id}.svg`;
      picture.alt = "";
      element.append(picture);
    } else element.textContent = Array.from(displayName(user))[0] || "影";
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  function presence(user, data) {
    // 宿主未提供可靠的真人在线信号；影片是否在播、远端自述或同步时间都不能推断在线。
    const demo = user?.is_demo ? demoUsers().find((entry) => sameId(entry.user.id, user.user_id || user.id)) : null;
    const value = user?.is_demo ? user.presence || data?.presence || demo?.user.presence || demo?.data?.presence : null;
    const kind = value?.online === true ? "online" : value?.online === false ? "offline" : "unknown";
    const label = kind === "online" ? "在线" : kind === "offline" ? "离线" : "状态未知";
    const element = node("span", `presence is-${kind}`);
    element.append(node("span", "presence-dot"), document.createTextNode(label));
    if (kind === "offline" && value.last_seen) element.title = `最近在线：${dateText(value.last_seen)}`;
    return element;
  }

  function emptyState(heading, description, symbol = "circle-alert") {
    const element = node("div", "empty-state");
    const icon = window.MovieFriendsIcons.create(symbol,"empty-symbol");
    icon.setAttribute("aria-hidden", "true");
    element.append(icon, node("h3", "", heading), node("p", "", description));
    return element;
  }

  function showLoginRequired() {
    state.sessionVersion += 1;
    state.detailVersion += 1;
    state.directoryVersion += 1;
    state.mediaVersion += 1;
    window.MovieFriendsHelp?.close();
    coverObserver.disconnect();
    titleCache.clear();
    state.user = null;
    state.friendId = null;
    state.friends = []; state.incoming = []; state.outgoing = [];
    state.directory = []; state.collections = []; state.browserCollections = false; state.demo = null; state.demoVersion += 1; state.demoSaving = false; state.lists = []; state.listDraft = []; state.listCover=null; state.favorites = []; state.publicFavorites = []; state.favoriteDraft = []; state.editorVersion += 1; state.listSearchVersion += 1; state.pendingPreview = null;
    clearTimeout(toastTimer);
    byId("toast").hidden = true;
    byId("workspace").hidden = true;
    byId("auth-required").hidden = false;
    byId("startup-state").hidden = true;
    for (const id of ["friend-list", "friend-detail", "incoming-requests", "outgoing-requests", "directory-list", "collection-list", "plugin-list-list", "demo-collection-list", "favorites-list", "public-card-preview", "sharing-scope", "preview-content", "media-dialog-content", "list-search-results", "list-draft-items", "list-target-options"]) byId(id).replaceChildren();
    window.MovieFriendsFeed?.reset();
    window.MovieFriendsCommunity?.reset();
    window.MovieFriendsNetwork?.reset();
    window.MovieFriendsAvailability?.reset();
    window.MovieFriendsProfile?.reset();
    window.MovieFriendsDisplayPolicy?.reset();
    state.editingList = null;
    byId("demo-collections").hidden = true;
    renderDemoControl();
    byId("invite-code").textContent = "";
    byId("invite-input").value = "";
    byId("privacy-form").reset();
    byId("list-create-form").reset();
    for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
    finishAddressConsent(false);
  }

  function populateProfile() {
    const user = state.user;
    byId("nickname-input").value = displayName(user);
    byId("discoverable-input").checked = Boolean(user.discoverable);
    byId("public-recent-input").checked = user.public_recent !== false;
    byId("share-favorites-input").checked = Boolean(user.share_favorites);
    byId("share-current-input").checked = Boolean(user.share_current);
    byId("share-history-input").checked = Boolean(user.share_history);
    byId("share-posts-input").checked = Boolean(user.share_posts);
    byId("greeting").textContent = `${displayName(user)}，欢迎回来。你的分享，由你选择。`;
    // 邀请码可能编码了连接信息，只提供复制动作，不在页面展开其内容。
    byId("invite-code").textContent = user.invite_code ? "你的专属邀请码已就绪" : "邀请码暂不可用";
    byId("copy-invite").disabled = !user.invite_code;
    byId("directory-notice").replaceChildren(node("span","",user.discoverable?"名片已公开":"名片未公开"),window.MovieFriendsHelp.create("我的公开名片",user.discoverable?"可在设置中预览、更新或撤下公开名片；近期观看和喜欢的电影按你的独立授权展示。":"仍可浏览广场。需要展示自己时，去设置选择内容并预览确认。"));
    window.MovieFriendsSettings.bind(byId("privacy-form"),{key:"sharing",session:state.sessionVersion,onRestore:renderPrivacySummary});
    renderPrivacySummary();
    renderFavorites();
  }

  function settingsAnchor() {
    let hash=window.location.hash;
    if(!hash){try{hash=window.parent.location.hash;}catch{/* 嵌入来源不同时只使用本页锚点。 */}}
    const id=hash.slice(1);
    return ["community-settings","watch-source-settings","display-policy-settings",
      "sharing-settings","network-settings","demo-settings","community-admin"].includes(id)?byId(id):null;
  }
  window.addEventListener("hashchange",()=>{
    const target=settingsAnchor();
    if(target&&state.user){if(state.tab!=="privacy")switchTab("privacy");target.scrollIntoView({block:"start"});}
  });

  async function startSession() {
    const version = state.sessionVersion;
    const result = await api("/me");
    if (version !== state.sessionVersion) return;
    state.user = result.user;
    state.sessionVersion += 1;
    const activeVersion = state.sessionVersion;
    byId("startup-state").hidden = true;
    byId("auth-required").hidden = true;
    byId("workspace").hidden = false;
    populateProfile();
    renderDetailEmpty();
    if(settingsAnchor())state.tab="privacy";
    switchTab(state.tab, false);
    await refreshSocial();
    if (state.user && activeVersion === state.sessionVersion) await window.MovieFriendsCommunity?.start().catch((error) => { if (!error.staleSession && activeVersion === state.sessionVersion) toast(safeError(error), true); });
    if (!state.user || activeVersion !== state.sessionVersion) return;
    if (state.tab === "profile") await window.MovieFriendsProfile?.load();
    if (state.tab === "privacy") await Promise.all([window.MovieFriendsProfile?.loadSettings(),window.MovieFriendsDisplayPolicy?.load(false),window.MovieFriendsNetwork?.settings()]);
    if (state.tab === "directory") await loadDirectory();
    if (state.tab === "collections") await loadCollectionSpace();
    if (state.tab === "privacy") { await window.MovieFriendsProfile?.loadSettings(); await loadFavorites(); await window.MovieFriendsDisplayPolicy?.load(); await window.MovieFriendsNetwork?.settings(); settingsAnchor()?.scrollIntoView({block:"start"}); }
    if (state.tab === "feed") await window.MovieFriendsFeed?.load();
  }

  function switchTab(tab, load = true) {
    window.MovieFriendsHelp?.close();
    state.tab = tab;
    const refresh=byId("refresh-button");
    (tab==="privacy"?byId("panel-privacy").querySelector(".section-intro"):document.querySelector(".workspace-tools")).append(refresh);
    refresh.querySelector("span:last-child").textContent=tab==="privacy"?"刷新设置":"刷新动态";
    byId("workspace").dataset.activeTab = tab;
    for (const item of document.querySelectorAll("[data-tab]")) {
      const active = item.dataset.tab === tab;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    }
    const activeTab=document.querySelector(".tab.is-active"),tabStrip=document.querySelector(".tabs");
    if(activeTab&&tabStrip&&tabStrip.scrollWidth>tabStrip.clientWidth)tabStrip.scrollLeft=activeTab.offsetLeft-tabStrip.offsetLeft-(tabStrip.clientWidth-activeTab.offsetWidth)/2;
    for (const item of document.querySelectorAll(".tab-panel")) item.hidden = item.id !== `panel-${tab}`;
    if (!load) return;
    const version = state.sessionVersion;
    const report = (error) => { if (version === state.sessionVersion && !error.staleSession) toast(safeError(error), true); };
    if (tab === "profile") Promise.resolve(window.MovieFriendsProfile?.load()).catch(report);
    if (tab === "directory") loadDirectory().catch(report);
    if (tab === "collections") loadCollectionSpace().catch(report);
    if (tab === "privacy") { Promise.resolve(window.MovieFriendsProfile?.loadSettings()).catch(report); loadFavorites().catch(report); Promise.resolve(window.MovieFriendsDisplayPolicy?.load()).catch(report); Promise.resolve(window.MovieFriendsNetwork?.settings()).catch(report); }
    if (tab === "feed") Promise.resolve(window.MovieFriendsFeed?.load()).catch(report);
  }

  function refreshSocial() {
    if (!state.user) return Promise.resolve();
    if (state.refreshing === state.sessionVersion) return state.refreshPromise;
    const version = state.sessionVersion;
    const demoVersion = state.demoVersion;
    state.refreshing = version;
    state.refreshPromise = readSocial(version, demoVersion);
    return state.refreshPromise;
  }

  async function readSocial(version, demoVersion) {
    try {
      const results = await Promise.allSettled([api("/friends"), api("/requests"), api("/demo")]);
      if (!state.user || version !== state.sessionVersion || demoVersion !== state.demoVersion) return;
      if (demoVersion === state.demoVersion && !state.demoSaving) state.demo = results[2].status === "fulfilled" ? results[2].value : null;
      renderDemoControl();
      if (results[0].status === "fulfilled") state.friends = list(results[0].value.friends);
      renderFriends();
      renderDemoCollections();
      window.MovieFriendsFeed?.refreshDemo();
      window.MovieFriendsCommunity?.refreshDemo();
      if (results[1].status === "fulfilled") {
        state.incoming = list(results[1].value.incoming);
        state.outgoing = list(results[1].value.outgoing);
        renderRequests();
      }
      const failed = results.find((result) => result.status === "rejected");
      if (failed) throw failed.reason;
      byId("last-refreshed").textContent = `更新于 ${dateText(new Date())} · 每 30 秒刷新`;
    } finally { if (state.refreshing === version) state.refreshing = null; }
  }

  function requestPerson(request) {
    const element = node("div", "person-copy");
    const name = node("strong", "", displayName(request));
    if (request.is_demo) name.append(node("span", "demo-badge", "演示"));
    element.append(name);
    element.append(presence(request));
    return element;
  }

  function renderRequests() {
    const incoming = state.incoming.filter((request) => (!request.status || request.status === "pending") && (!request.is_demo || state.demo?.enabled));
    if (incoming.length && byId("request-count").textContent === "0") byId("requests-panel").open = true;
    byId("request-count").textContent = incoming.length;
    byId("incoming-requests").replaceChildren(...incoming.map((request) => {
      const row = node("div", "request-row");
      const actions = node("div", "row-actions");
      actions.append(button("接受", "button-primary button-small", async () => {
        const version = state.sessionVersion;
        if (!await addressConsent("接受好友申请", displayName(request), Boolean(request.is_demo))) return;
        if (version !== state.sessionVersion) return;
        await api(`/requests/${encodeURIComponent(request.id)}/accept`, { method: "POST", body: { address_consent: true } });
        if (version !== state.sessionVersion) return;
        toast(request.is_demo ? "已添加演示好友，身份与观看行为为模拟，影片资料来自真实作品。" : "已成为好友，彼此可以查看已授权的内容。");
        await refreshSocial();
      }), button("拒绝", "button-quiet button-small", async () => {
        const version = state.sessionVersion;
        await api(`/requests/${encodeURIComponent(request.id)}/reject`, { method: "POST" });
        if (version !== state.sessionVersion) return;
        toast("已拒绝该申请。"); await refreshSocial();
      }));
      const profile = button("", "person-link request-profile-link", () => openPerson(request, { relationship_id: request.id, is_remote: request.is_remote, status: request.status }));
      profile.append(avatar(request, true), requestPerson(request));
      row.append(profile, actions);
      return row;
    }));
    if (!incoming.length) byId("incoming-requests").append(node("p", "empty-inline", "暂时没有新的申请。可以去广场看看，或邀请一位朋友。"));
    const outgoing = state.outgoing.filter((request) => !request.is_demo || state.demo?.enabled);
    byId("outgoing-count").textContent = outgoing.length;
    const labels = { pending: "等待对方同意", accepted: "已同意", rejected: "对方未同意", revoked: "已撤销" };
    byId("outgoing-requests").replaceChildren(...outgoing.map((request) => {
      const row = node("div", "request-row");
      const profile = button("", "person-link request-profile-link", () => openPerson(request, { relationship_id: request.id, is_remote: request.is_remote, status: request.status }));
      profile.append(avatar(request), requestPerson(request));
      row.append(profile, node("small", "muted", labels[request.status] || "等待对方处理"));
      return row;
    }));
    if (!state.outgoing.length) byId("outgoing-requests").append(node("p", "empty-inline", "你还没有发出申请。"));
    if (state.directory.length) renderDirectory();
  }

  function renderFriends() {
    const friends = state.friends.filter((friend) => !friend.is_demo || state.demo?.enabled);
    const demos = demoUsers().filter(({ user }) => !friends.some((friend) => friend.is_demo && sameId(friend.user_id, user.id)));
    const choices = new Set([...friends.map((friend) => String(friend.id)), ...demos.map(({ user }) => DEMO_PREVIEW_ID + user.id)]);
    byId("friend-count").textContent = friends.filter((friend) => !friend.is_demo).length;
    if (state.friendId !== null && !choices.has(String(state.friendId))) {
      state.friendId = null;
      state.detailVersion += 1;
      renderDetailEmpty();
    }
    if (state.friendId === null && !friends.length && demos.length) state.friendId = DEMO_PREVIEW_ID + demos[0].user.id;
    const target = byId("friend-list");
    target.replaceChildren(...friends.map((friend) => {
      const element = button("", "friend-button", async () => {
        state.friendId = friend.id;
        renderFriends();
        await loadFriendDetail(false);
      });
      element.classList.toggle("is-selected", sameId(friend.id, state.friendId));
      element.setAttribute("aria-pressed", String(sameId(friend.id, state.friendId)));
      const copy = requestPerson(friend);
      element.append(avatar(friend), copy, node("span", "friend-chevron", "›"));
      return element;
    }));
    if (demos.length) target.append(node("p", "rail-label", "探索演示好友"));
    for (const demo of demos) {
      const id = DEMO_PREVIEW_ID + demo.user.id;
      const preview = button("", "friend-button demo-preview-button", () => {
        state.friendId = id;
        renderFriends();
      });
      preview.classList.toggle("is-selected", state.friendId === id);
      preview.setAttribute("aria-pressed", String(state.friendId === id));
      const copy = requestPerson(demo.user);
      preview.append(avatar(demo.user), copy, node("span", "friend-chevron", "›"));
      target.append(preview);
      if (state.friendId === id) renderFriendDetail({ friend: demo.user, data: demo.data, readonly: true });
    }
    if (!friends.length && !demos.length) target.append(emptyState("为同好留个位置", "去广场认识朋友，或分享邀请码。", "users-round"));
  }

  function renderDetailEmpty() {
    byId("friend-detail").replaceChildren(emptyState("看看好友的观影世界", "选择一位好友，查看对方正在看的电影、观影记录和共享片单。", "clapperboard"));
  }

  async function loadFriendDetail(background = false) {
    if (state.friendId === null || !state.user) return;
    if (String(state.friendId).startsWith(DEMO_PREVIEW_ID)) {
      const demo = demoUsers().find(({ user }) => DEMO_PREVIEW_ID + user.id === state.friendId);
      if (demo) renderFriendDetail({ friend: demo.user, data: demo.data, readonly: true });
      return;
    }
    const id = state.friendId;
    const version = ++state.detailVersion;
    if (!background) byId("friend-detail").replaceChildren(node("p", "busy-text", "正在读取好友分享的内容…"));
    try {
      const result = await api(`/friends/${encodeURIComponent(id)}`);
      if (version !== state.detailVersion || !state.user || !sameId(id, state.friendId)) return;
      renderFriendDetail(result);
    } catch (error) {
      if (version !== state.detailVersion || !state.user) return;
      // 后台同步失败也立即移除旧快照，避免离线后仍把过期状态展示为正在观看。
      byId("friend-detail").replaceChildren(emptyState("暂时无法获取好友动态", safeError(error)), button("重新获取", "button-outline", () => loadFriendDetail(false)));
    }
  }

  function renderFriendDetail(result) {
    const friend = result.friend || state.friends.find((item) => sameId(item.id, state.friendId));
    const isDemo = Boolean(friend?.is_demo || result.data?.is_demo);
    const header = node("div", "detail-heading");
    const copy = node("div", "person-copy");
    const name = node("h3", "", displayName(friend));
    if (isDemo) name.append(node("span", "demo-badge", "演示"));
    copy.append(name, presence(friend, result.data));
    const person = button("", "person-link detail-person-link", () => openPerson(friend, { relationship_id: result.readonly ? null : friend.id, is_remote: friend.is_remote }));
    person.append(avatar({ ...friend, avatar_data: friend.is_remote ? result.data?.avatar_data : friend.avatar_data }), copy);
    header.append(person);
    if (!result.readonly) header.append(button("删除好友", "button-quiet", async () => {
      const version = state.sessionVersion;
      if (!window.confirm(`删除与「${displayName(friend)}」的好友关系？删除后双方无法再获取彼此的共享数据，已经被保存的内容无法收回。`)) return;
      await api(`/friends/${encodeURIComponent(friend.id)}`, { method: "DELETE" });
      if (version !== state.sessionVersion) return;
      state.friendId = null;
      renderDetailEmpty();
      toast("好友关系已删除，后续访问已停止。");
      await refreshSocial();
    }));
    const target = byId("friend-detail");
    target.replaceChildren(header);
    if (isDemo) target.append(node("p", "demo-disclaimer", "演示用户与观看行为为模拟，影片信息来自真实作品。"));
    if (result.readonly) target.append(node("p", "readonly-caption", "只读预览 · 无需添加好友，不会开启你的任何分享"));
    if (result.error) target.append(node("p", "detail-error", safeError({ message: result.error })));
    appendSharedData(target, result.data || {}, isDemo, friend);
  }

  function availableDemo() {
    return state.demo?.enabled ? state.demo : null;
  }

  function demoUsers() {
    const demo = availableDemo();
    return demo ? (list(demo.users).length ? list(demo.users) : demo.user ? [{ user: demo.user, data: demo.data }] : []) : [];
  }

  function renderDemoControl() {
    const control = byId("demo-toggle");
    control.checked = Boolean(state.demo?.enabled);
    control.disabled = state.demoSaving || !state.demo || state.demo.available === false;
    control.setAttribute("aria-busy", String(state.demoSaving));
    byId("demo-status").textContent = state.demoSaving ? "正在保存…" : !state.demo ? "偏好暂不可用" : state.demo.available === false ? "管理员已关闭" : state.demo.enabled ? "当前账号已开启" : "当前账号已关闭";
  }

  async function saveDemoPreference(enabled) {
    if (state.demoSaving) return;
    const version = state.sessionVersion;
    const previous = state.demo;
    state.demoVersion += 1;
    state.demoSaving = true;
    renderDemoControl();
    byId("demo-toggle").checked = enabled;
    let saved = false;
    try {
      const result = await api("/demo/preferences", { method: "PATCH", body: { enabled } });
      requireSession(version);
      state.demo = result;
      saved = true;
      if (!result.enabled) {
        state.friends = state.friends.filter((friend) => !friend.is_demo);
        state.incoming = state.incoming.filter((friend) => !friend.is_demo);
        state.outgoing = state.outgoing.filter((friend) => !friend.is_demo);
        state.directory = state.directory.filter((user) => !user.is_demo);
        if (byId("media-dialog").dataset.demo === "true") byId("media-dialog").close();
      }
      renderFriends(); renderRequests(); renderDirectory(); renderDemoCollections();
      window.MovieFriendsFeed?.refreshDemo();
      window.MovieFriendsCommunity?.refreshDemo();
      toast(result.enabled ? "已开启演示内容。" : "已关闭演示内容，真实好友与片单保持原样。");
    } catch (error) {
      if (!state.user || version !== state.sessionVersion || error.staleSession) return;
      state.demo = previous;
      toast(`演示偏好未保存，已恢复原设置。${safeError(error)}`, true);
    } finally {
      if (version === state.sessionVersion) { state.demoSaving = false; renderDemoControl(); }
    }
    if (saved && state.user && version === state.sessionVersion) {
      // 三个入口共同服从账号偏好，关闭只隐藏演示，绝不删除关系或个人片单。
      await state.refreshPromise?.catch(() => {});
      if (!state.user || version !== state.sessionVersion) return;
      await Promise.allSettled([refreshSocial(), loadDirectory(), loadCollectionSpace()]);
    }
  }

  function renderDemoCollections() {
    const demo = availableDemo();
    byId("demo-collections").hidden = !demo;
    const collections = demoUsers().flatMap(({ user, data }) => list(data?.collections).map((collection) => ({ ...collection, author: user })));
    byId("demo-collection-list").replaceChildren(...(demo ? [sharedCollections(collections, true)] : []));
  }

  function currentCard(record, isDemo = false, observedAt) {
    const card = node("div", "now-card");
    const status = node("div", "now-label");
    const label = record.paused || record.state === "paused" ? "已暂停" : "正在观看";
    status.append(node("span", "status-dot"), document.createTextNode(isDemo ? `演示 · ${label}` : label));
    const body = node("div", "now-media");
    const copy = node("div", "person-copy");
    const heading = node("h4"); heading.append(mediaTitle(record, isDemo));
    copy.append(heading);
    body.append(mediaPoster(record, isDemo), copy);
    card.append(status, body);
    const meta = mediaMeta(record);
    if (meta) copy.append(node("p", "media-meta", meta));
    const details = isDemo ? state.demo?.details?.[media(record).demo_id] : null;
    if (details?.overview) copy.append(node("p", "now-overview", details.overview));
    const position = record.position_ms ?? record.end_position_ms ?? record.start_position_ms;
    const duration = record.duration_ms;
    const rawProgress = record.progress_percent ?? (Number(duration) > 0 && position != null ? Number(position) / Number(duration) * 100 : null);
    if (rawProgress !== null && Number.isFinite(Number(rawProgress))) {
      const progress = Math.max(0, Math.min(100, Number(rawProgress)));
      const track = node("div", "progress-track");
      track.setAttribute("role", "progressbar");
      track.setAttribute("aria-label", "观看进度");
      track.setAttribute("aria-valuenow", String(Math.round(progress)));
      track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100");
      const fill = node("div", "progress-fill"); fill.style.width = `${progress}%`; track.append(fill);
      const caption = node("p", "progress-caption");
      caption.append(node("span", "", `${durationText(position)} / ${durationText(duration)}`), node("span", "", `${Math.round(progress)}%`));
      card.append(track, caption);
      if (isDemo && Number(duration) > 0 && Number.isFinite(Number(position)) && !record.paused && record.state !== "paused") {
        card.dataset.demoClock = "1";
        card.demoClock = { position: Number(position), duration: Number(duration), rate: Number(record.playback_rate) > 0 ? Number(record.playback_rate) : 1, observed: Number.isFinite(Date.parse(observedAt)) ? Date.parse(observedAt) : Date.now(), fill, track, caption };
      }
    }
    return card;
  }

  function historyList(records, limit = 100, isDemo = false) {
    const container = node("div", "scroll-list");
    for (const record of list(records).slice(0, limit)) {
      const row = node("div", "history-row");
      const copy = node("div", "person-copy");
      const heading = node("h4"); heading.append(mediaTitle(record, isDemo));
      copy.append(heading);
      const meta = mediaMeta(record);
      if (meta) copy.append(node("p", "media-meta", meta));
      const watchedAt = record.started_at ?? record.ended_at ?? record.observed_at;
      row.append(mediaPoster(record, isDemo), copy, node("span", "history-date", dateText(watchedAt)));
      container.append(row);
    }
    if (!list(records).length) container.append(node("p", "empty-inline", "暂无观影记录。"));
    return container;
  }

  function sharedCollections(collections, isDemo = false, author = null) {
    const container = node("div", "shared-collections-grid");
    for (const collection of list(collections)) {
      const card = node("article", "shared-collection-card");
      const details = node("details", "shared-list");
      const summary = node("summary", "", collection.name || "未命名片单");
      const items = list(collection.items);
      summary.append(node("span", "", `${collection.item_count ?? items.length} 部影片`));
      if (isDemo && !author && !collection.author) summary.append(node("span", "demo-badge", "演示"));
      details.append(summary);
      if (collection.description) details.append(node("p", "collection-description", collection.description));
      details.append(mediaItems(items, isDemo));
      card.append(collectionCover(collection, isDemo));
      const creator = author || collection.author;
      if (isDemo && creator) {
        const byline = node("div", "collection-author");
        const profile = button("", "person-link", () => openPerson(creator));
        profile.append(avatar(creator), node("span", "", displayName(creator)));
        byline.append(profile, node("span", "demo-badge", "演示"));
        card.append(byline);
      }
      card.append(details);
      container.append(card);
    }
    if (!list(collections).length) container.append(node("p", "empty-inline", "暂未分享片单。"));
    return container;
  }

  function appendSharedData(target, data, isDemo = Boolean(data.is_demo), author = null) {
    const current = node("section", "detail-section");
    current.append(node("h3", "", "正在观看"));
    current.append(data.sharing?.current === false ? node("p", "empty-inline", "尚未授权分享正在观看的内容。") : data.current ? currentCard(data.current, isDemo, data.observed_at) : node("p", "empty-inline", "目前没有正在观看的影片。"));
    const history = node("section", "detail-section");
    history.append(node("h3", "", "观影历史"), data.sharing?.history === false ? node("p", "empty-inline", "尚未授权分享观影历史。") : historyList(data.history, 100, isDemo));
    const collections = node("section", "detail-section");
    collections.append(node("h3", "", "共享片单"), sharedCollections(data.collections, isDemo, author));
    target.append(current, history);
    if (list(data.favorites).length) {
      const favorites = node("section", "detail-section");
      favorites.append(node("h3", "", "喜欢的电影"), movieShelf(data.favorites, isDemo));
      target.append(favorites);
    }
    target.append(collections, node("p", "detail-footnote", data.observed_at ? `${isDemo ? "演示资料时间：" : "数据同步于 "}${dateText(data.observed_at)}` : "尚无同步时间"));
  }

  function finishAddressConsent(accepted) {
    if (byId("address-dialog").open) byId("address-dialog").close();
    const resolve = consentResolve;
    consentResolve = null;
    if (resolve) resolve(accepted);
  }

  function addressConsent(action, nickname, isDemo = false) {
    finishAddressConsent(false);
    byId("address-title").textContent = action === "接受好友申请" ? "接受好友申请前，请确认" : "发送好友申请前，请确认";
    byId("address-target").textContent = nickname ? `你正在${action}：${nickname}` : "你正在通过邀请码发送好友申请。";
    byId("address-demo-notice").hidden = !isDemo;
    byId("address-real-notice").hidden = isDemo;
    byId("address-check-label").textContent = isDemo ? "我已了解这是本地演示身份，影片为真实作品、观看行为为演示，同意创建演示好友关系。" : "我已了解服务器地址与已授权数据可能被对方记录，同意继续。";
    byId("address-checkbox").checked = false;
    byId("confirm-address").disabled = true;
    byId("confirm-address").textContent = action === "接受好友申请" ? "同意并接受申请" : "同意并发送申请";
    window.MovieFriendsUI.open(byId("address-dialog"));
    return new Promise((resolve) => { consentResolve = resolve; });
  }

  async function sendRequest(payload, nickname, isDemo = false) {
    const version = state.sessionVersion;
    if (!await addressConsent("发送好友申请", nickname, isDemo)) return false;
    if (version !== state.sessionVersion) return false;
    await api("/requests", { method: "POST", body: { ...payload, address_consent: true } });
    if (version !== state.sessionVersion) return false;
    toast(isDemo ? "虚拟演示用户已由本插件自动同意，分享开关保持原样。" : "好友申请已发送，等待对方同意。");
    await refreshSocial();
    return version === state.sessionVersion;
  }

  async function loadDirectory() {
    const sessionVersion = state.sessionVersion;
    const version = ++state.directoryVersion;
    if (!state.directory.length) byId("directory-list").replaceChildren(node("p", "busy-text", "正在寻找观影同好…"));
    try {
      const result = await api("/directory");
      if (!state.user || sessionVersion !== state.sessionVersion || version !== state.directoryVersion) return;
      state.directory = list(result.users);
      renderDirectory();
    } catch (error) {
      if (!state.user || sessionVersion !== state.sessionVersion || version !== state.directoryVersion) return;
      state.directory = [];
      byId("directory-list").replaceChildren(emptyState("广场暂时无法加载", safeError(error)), button("重新获取广场", "button-outline", loadDirectory));
      throw error;
    }
  }

  function renderDirectory() {
    const users = state.directory.filter((user) => !sameId(user.id, state.user?.id) && (!user.is_demo || state.demo?.enabled));
    byId("directory-list").replaceChildren(...users.map((user, index) => {
      const card = node("article", "panel directory-card");
      if (user.network_public) {
        card.classList.add("network-card");
        window.MovieFriendsNetwork.renderCard(user, card);
        card.append(button("查看影友主页", "button-quiet", () => openPerson(user)));
      } else {
      const heading = node("div", "directory-person");
      const name = node("h3", "", displayName(user));
      if (user.is_demo) name.append(node("span", "demo-badge", "演示"));
      const person = node("div", "person-copy");
      person.append(name, presence(user));
      const profile = button("", "person-link directory-profile-link", () => openPerson(user));
      profile.append(avatar(user, index % 2 === 1), person);
      heading.append(profile);
      card.append(heading);
      if (list(user.history).length) card.append(node("p", "card-eyebrow", "近期观看"), historyList(user.history, 5, Boolean(user.is_demo)));
      if (list(user.favorites).length) card.append(node("p", "card-eyebrow directory-favorites-heading", "喜欢的电影"), movieShelf(user.favorites, Boolean(user.is_demo)));
      if (!list(user.history).length && !list(user.favorites).length) card.append(node("p", "empty-inline", "暂未公开观影内容。"));
      }
      // 好友列表的 id 是关系 ID，不能拿它和广场用户 ID 比较。
      const isFriend = state.friends.some((friend) => friend.user_id && sameId(friend.user_id, user.id));
      const requested = state.outgoing.some((request) => request.user_id && sameId(request.user_id, user.id) && request.status === "pending");
      const add = button(isFriend ? "已经是好友" : requested ? "申请已发送" : user.is_demo ? "添加虚拟演示好友" : "申请添加好友", isFriend || requested ? "button-outline" : "button-primary", () => sendRequest({ user_id: user.id }, displayName(user), Boolean(user.is_demo)));
      add.disabled = isFriend || requested;
      card.append(add);
      return card;
    }));
    if (!users.length) byId("directory-list").append(emptyState("广场还很安静", "暂时没有其他用户公开信息。也可以用邀请码邀请朋友。", "users-round"));
  }

  async function loadCollectionSpace() {
    await Promise.all([loadCollections(), loadLists(), loadFavorites()]);
  }

  async function loadLists() {
    const version = state.sessionVersion;
    try {
      const result = await api("/lists");
      requireSession(version);
      state.lists = list(result.lists);
      byId("list-notice").hidden = true;
      renderLists();
    } catch (error) {
      if (!state.user || version !== state.sessionVersion || error.staleSession) return;
      byId("list-notice").hidden = false;
      byId("list-notice").textContent = `插件片单暂时无法刷新。${safeError(error)}`;
    }
  }

  function renderLists() {
    byId("plugin-list-list").replaceChildren(...state.lists.map((collection) => {
      const card = node("article", "panel collection-card plugin-list-card");
      card.append(collectionCover(collection), node("h3", "", collection.name), node("p", "muted", `${list(collection.items).length} 部影片 · ${collection.shared ? "已分享给好友" : "仅自己可见"}`));
      if (collection.description) card.append(node("p", "collection-description", collection.description));
      const contents = node("details", "own-collection-items");
      contents.append(node("summary", "", "查看片单影片"), mediaItems(collection.items));
      card.append(contents);
      const actions = node("div", "collection-actions");
      actions.append(button("编辑片单", "button-outline", () => openListEditor(collection)));
      actions.append(button(collection.shared ? "停止分享" : "分享给好友", collection.shared ? "button-outline" : "button-primary", async () => {
        const version = state.sessionVersion;
        if (!window.confirm(collection.shared ? `停止分享「${collection.name}」？` : `将「${collection.name}」的 ${list(collection.items).length} 部影片分享给已同意的好友？`)) return;
        await api(`/lists/${encodeURIComponent(collection.id)}/share`, { method: collection.shared ? "DELETE" : "POST" });
        requireSession(version);
        toast(collection.shared ? "已停止分享该片单。" : "片单已分享给好友。");
        await loadLists();
      }), button("删除片单", "button-quiet", async () => {
        const version = state.sessionVersion;
        if (!window.confirm(`删除「${collection.name}」？这会删除插件中保存的片单并停止后续分享。`)) return;
        await api(`/lists/${encodeURIComponent(collection.id)}`, { method: "DELETE" });
        requireSession(version);
        toast("片单已删除。");
        await loadLists();
      }));
      if (collection.shared) actions.insertBefore(button("更新共享快照", "button-outline", async () => {
        const version = state.sessionVersion;
        if (!window.confirm(`将「${collection.name}」当前的 ${list(collection.items).length} 部影片更新为好友可见的共享快照？`)) return;
        await api(`/lists/${encodeURIComponent(collection.id)}/share`, { method: "POST" });
        requireSession(version);
        toast("共享快照已更新，好友可以看到这次确认的影片。");
        await loadLists();
      }), actions.lastChild);
      card.append(actions);
      return card;
    }));
    if (!state.lists.length) byId("plugin-list-list").append(emptyState("创建第一份片单", "收藏喜欢的故事，创建后仅自己可见。", "list-video"));
  }

  function portableMedia(item) {
    const result = {};
    for (const key of ["media_item_id", "tmdb_id", "kind", "title", "year", "season_number", "episode_number", "episode_title"]) if (Object.hasOwn(item, key)) result[key] = item[key];
    const path = posterPath(item);
    if (path) result.poster_path = path;
    return result;
  }

  function editorDraft() {
    return state.editorMode === "favorites" ? state.favoriteDraft : state.listDraft;
  }

  function addDraftItem(item) {
    const draft = editorDraft();
    if (draft.length >= 500) throw new Error("最多选择 500 部影片。");
    const copy = portableMedia(item);
    const key = (value) => titleKey(value) || `${value.kind || ""}:${value.title || ""}:${value.year || ""}`;
    if (draft.some((value) => key(value) === key(copy))) return;
    draft.push(copy);
    renderListDraft();
  }

  function renderListCover() {
    let root=byId("list-cover-editor");if(!root){root=node("section");root.id="list-cover-editor";byId("list-metadata").after(root);}
    root.replaceChildren();root.hidden=state.editorMode!=="list";if(root.hidden)return;
    if(state.listCover&&!state.listDraft.some(item=>titleKey(item)===titleKey(state.listCover)))state.listCover=null;
    const details=node("details","list-cover-editor"),summary=node("summary","","片单封面"),current=node("div","list-cover-current"),films=node("div","watch-row-actions"),picker=node("div","feed-cover-picker");picker.hidden=true;details.append(summary,current,films,picker);
    function preview(){current.replaceChildren();if(state.listCover)current.append(window.MovieFriendsArtwork.gallery([{path:state.listCover.cover_path,type:state.listCover.cover_type}]));else current.append(node("p","muted","自动封面 · 从片单影片组合生成"));}
    preview();films.append(button("恢复自动封面","button-quiet",()=>{state.listCover=null;preview();picker.replaceChildren();picker.hidden=true;}));
    for(const item of state.listDraft){const choose=button(title(item),"button-outline",()=>{picker.coverPicker?.destroy();picker.coverPicker=window.MovieFriendsArtwork.picker(picker,item,{max:1,title:`为「${title(item)}」选择封面`,selected:state.listCover&&titleKey(item)===titleKey(state.listCover)?[{path:state.listCover.cover_path,type:state.listCover.cover_type}]:[],onChange(images){state.listCover=images.length?{...portableMedia(item),cover_path:images[0].path,cover_type:images[0].type}:null;preview();}});});choose.setAttribute("aria-label",`从${title(item)}选择片单封面`);films.append(choose);}
    root.append(details);
  }

  function renderListDraft() {
    renderListCover();
    const draft = editorDraft();
    byId("list-draft-count").textContent = draft.length;
    const contents = mediaItems(draft);
    [...contents.children].forEach((row, index) => {
      if (!draft[index]) return;
      if (state.editorMode === "list" && draft.length > 1) {
        const up = button("↑", "button-quiet button-small reorder-item", () => {
          [draft[index - 1], draft[index]] = [draft[index], draft[index - 1]];
          renderListDraft();
        });
        up.disabled = index === 0; up.setAttribute("aria-label", `上移${title(draft[index])}`);
        const down = button("↓", "button-quiet button-small reorder-item", () => {
          [draft[index], draft[index + 1]] = [draft[index + 1], draft[index]];
          renderListDraft();
        });
        down.disabled = index === draft.length - 1; down.setAttribute("aria-label", `下移${title(draft[index])}`);
        row.append(up, down);
      }
      row.append(button("移除", "button-quiet button-small", () => {
        draft.splice(index, 1);
        renderListDraft();
      }));
    });
    byId("list-draft-items").replaceChildren(contents);
  }

  function openListCreator() {
    if (state.editingList) { state.listDraft = []; state.listCover=null; byId("list-name").value = ""; byId("list-description").value = ""; }
    state.editingList = null;
    state.editorMode = "list";
    configureEditor();
    if (!byId("list-create-dialog").open) window.MovieFriendsUI.open(byId("list-create-dialog"));
  }

  async function openListEditor(collection) {
    const version = state.sessionVersion;
    const editorVersion = ++state.editorVersion;
    const result = await api("/lists");
    requireSession(version);
    if (editorVersion !== state.editorVersion) return;
    const current = list(result.lists).find((item) => sameId(item.id, collection.id));
    if (!current) throw new Error("片单已不存在，请刷新列表。");
    state.editingList = { id: current.id, revision: current.revision ?? 0, shared: Boolean(current.shared) };
    state.listDraft = list(current.items).map((item) => ({ ...item }));
    state.listCover = current.cover ? {...current.cover} : null;
    state.editorMode = "list";
    byId("list-name").value = current.name;
    byId("list-description").value = current.description || "";
    configureEditor();
    if (!byId("list-create-dialog").open) window.MovieFriendsUI.open(byId("list-create-dialog"));
  }

  function configureEditor() {
    state.editorVersion += 1;
    state.listSearchVersion += 1;
    const favorites = state.editorMode === "favorites";
    byId("list-metadata").hidden = favorites;
    byId("list-name").disabled = favorites;
    byId("list-create-title").textContent = favorites ? "编辑我喜欢的电影" : state.editingList ? "编辑片单" : "创建自己的片单";
    byId("list-editor-description").textContent = favorites ? "私人喜欢列表可反复修改，不会自动更新广场。公开需去设置重新预览确认。" : state.editingList ? "修改仅保存到私人片单。好友仍看到上次确认的共享快照，需另外更新分享。" : "片单只保存在本插件，默认为私有。可搜索选片，也可以先创建空片单。";
    byId("list-save-button").textContent = favorites ? "保存私人喜欢" : state.editingList ? "保存片单修改" : "创建私有片单";
    byId("list-search-results").replaceChildren();
    byId("list-search-notice").hidden = true;
    renderListDraft();
  }

  async function searchListTitles() {
    const query = byId("list-search-input").value.trim();
    if (!query) return;
    const version = state.sessionVersion;
    const searchVersion = ++state.listSearchVersion;
    byId("list-search-results").replaceChildren(node("p", "busy-text", "正在搜索影片…"));
    byId("list-search-notice").hidden = true;
    try {
      // 这是宿主的只读搜索，不保存历史，不以插件管理员身份代查。
      const result = await hostApi("/search/titles", { method: "POST", body: { query, provider: "tmdb", save_history: false } });
      requireSession(version);
      if (searchVersion !== state.listSearchVersion || query !== byId("list-search-input").value.trim()) return;
      const items = list(result.titles).filter((item) => ["movie", "tv"].includes(item.media_type) && Number.isSafeInteger(Number(item.external_id)) && Number(item.external_id) > 0).map((item) => portableMedia({ tmdb_id: Number(item.external_id), kind: item.media_type, title: item.title, year: item.release_year, poster_url: item.poster_url }));
      const results = mediaItems(items);
      [...results.children].forEach((row, index) => {
        if (items[index]) row.append(button("选择", "button-outline button-small", () => addDraftItem(items[index])));
      });
      byId("list-search-results").replaceChildren(items.length ? results : node("p", "empty-inline", "没有找到对应影片，请尝试其他名称。"));
    } catch (error) {
      if (!state.user || version !== state.sessionVersion || searchVersion !== state.listSearchVersion || error.staleSession) return;
      byId("list-search-results").replaceChildren();
      byId("list-search-notice").hidden = false;
      byId("list-search-notice").textContent = error.status === 403 ? "当前账号没有主程序的影片搜索权限。你仍可先创建空片单，或从已可见的影片详情加入待创建片单；如需搜索，请联系管理员。" : safeError(error);
    }
  }

  async function loadCollections() {
    const version = state.sessionVersion;
    if (!state.collections.length) byId("collection-list").replaceChildren(node("p", "busy-text", "正在读取你的片单…"));
    let snapshots = state.collections.filter((collection) => collection.shared);
    try {
      const result = await api("/collections");
      if (!state.user || version !== state.sessionVersion) return;
      snapshots = list(result.collections);
      state.browserCollections = Boolean(result.browser_required);
      let collections = snapshots;
      let sourceError = "";
      if (state.browserCollections) {
        try {
          // 原生合集接口始终以当前浏览器身份读取，不传成员 ID、访问令牌或插件写标记。
          const native = await hostApi("/collections?include_empty=true&include_hidden=true");
          if (!Array.isArray(native)) throw new Error("主程序返回的片单列表格式无效。");
          requireSession(version);
          const eligible = native.filter(canShareCollection);
          const shared = new Map(snapshots.map((collection) => [String(collection.id), collection]));
          collections = eligible.map((collection) => {
            const snapshot = shared.get(String(collection.id));
            return { ...collection, ...(snapshot ? { name: snapshot.name, item_count: snapshot.item_count ?? list(snapshot.items).length, items: snapshot.items } : {}), shared: Boolean(snapshot), source_available: true };
          });
          const available = new Set(eligible.map((collection) => String(collection.id)));
          collections.push(...snapshots.filter((collection) => !available.has(String(collection.id))).map((collection) => ({ ...collection, shared: true, source_available: false })));
        } catch (error) {
          if (!state.user || version !== state.sessionVersion || error.staleSession) return;
          sourceError = `暂时无法读取主程序片单。${safeError(error)} 已分享的快照仍可停止分享。`;
          collections = snapshots.map((collection) => ({ ...collection, shared: true, source_available: false }));
        }
      }
      if (!state.user || version !== state.sessionVersion) return;
      state.collections = collections;
      byId("collection-notice").textContent = sourceError;
      byId("collection-notice").hidden = !sourceError;
      renderCollections();
    } catch (error) {
      if (!state.user || version !== state.sessionVersion || error.staleSession) return;
      state.collections = snapshots.map((collection) => ({ ...collection, shared: true, source_available: false }));
      byId("collection-notice").textContent = `片单暂时无法刷新。${safeError(error)} 仍保留已加载快照的停止分享入口。`;
      byId("collection-notice").hidden = false;
      renderCollections();
    }
  }

  function canShareCollection(collection) {
    // 字段名来自原生 CollectionView；只允许当前查看者可管理的用户自建合集。
    return collection.manageable === true && collection.editable === true && collection.kind === "user" && collection.builtin === null;
  }

  function collectionContents(collection) {
    const details = node("details", "own-collection-items");
    details.append(node("summary", "", "查看片单影片"));
    const contents = node("div", "collection-contents");
    details.append(contents);
    let loaded = false;
    const version = state.sessionVersion;
    const items = [];
    async function loadPage(trigger) {
      if (trigger) trigger.remove();
      const busy = node("p", "busy-text", "正在读取片单影片…");
      contents.append(busy);
      try {
        const page = await hostApi(`/collections/${encodeURIComponent(collection.id)}/items?limit=20&offset=${items.length}`);
        requireSession(version);
        if (!Array.isArray(page)) throw new Error("主程序返回的片单内容格式无效。");
        items.push(...page);
        contents.replaceChildren(mediaItems(items));
        if (page.length === 20 && (!Number.isFinite(Number(collection.item_count)) || items.length < Number(collection.item_count))) {
          const more = button("加载更多影片", "button-quiet", () => loadPage(more));
          contents.append(more);
        }
      } catch (error) {
        if (!state.user || version !== state.sessionVersion || error.staleSession) return;
        busy.remove();
        const retry = button("重试读取影片", "button-quiet", () => loadPage(retry));
        contents.append(node("p", "empty-inline", safeError(error)), retry);
      }
    }
    details.addEventListener("toggle", () => {
      if (!details.open || loaded) return;
      loaded = true;
      if (collection.shared || collection.source_available === false || !state.browserCollections) contents.append(mediaItems(collection.items));
      else loadPage();
    });
    return details;
  }

  async function collectionSnapshot(collection) {
    const version = state.sessionVersion;
    const current = await hostApi(`/collections/${encodeURIComponent(collection.id)}`);
    requireSession(version);
    if (!canShareCollection(current)) throw new Error("该片单已不属于当前可分享的自建片单，请刷新列表。");
    if (Number(current.item_count) > 500) throw new Error("这份片单超过 500 部影片，无法完整分享。请先创建不超过 500 部的片单；不会截断分享。");
    const items = [];
    const seen = new Set();
    // 原生 /collections/{id}/items 的 data 是数组，每页最多 200 项。
    // 读到完整尾页后才提交；超过 500 或中途改变时拒绝生成不完整快照。
    for (let offset = 0; ; offset += 200) {
      const page = await hostApi(`/collections/${encodeURIComponent(collection.id)}/items?limit=200&offset=${offset}`);
      requireSession(version);
      if (!Array.isArray(page)) throw new Error("主程序返回的片单内容格式无效，尚未分享。");
      if (items.length + page.length > 500) throw new Error("这份片单超过 500 部影片，尚未分享。不会静默截断内容。");
      for (const item of page) {
        if (!Number.isSafeInteger(item.media_item_id) || seen.has(item.media_item_id)) throw new Error("片单在读取期间发生变化，请刷新后重试。");
        seen.add(item.media_item_id);
        // 只提交影片摘要，绝不把宿主返回的文件路径、资产/播放地址或其他私有字段外发。
        items.push(portableMedia(item));
      }
      if (page.length < 200) break;
    }
    if (Number.isFinite(Number(current.item_count)) && items.length !== Number(current.item_count)) throw new Error("片单数量在读取期间发生变化，请刷新后重试。");
    return { id: current.id, name: current.name, items };
  }

  function renderCollections() {
    byId("collection-list").replaceChildren(...state.collections.map((collection) => {
      const card = node("article", "panel collection-card");
      const caption = node("p", "muted", `${collection.item_count ?? list(collection.items).length} 部影片`);
      if (collection.shared) caption.append(node("span", "collection-shared", "已分享快照"));
      if (collection.source_available === false) caption.append(node("span", "collection-source-note", "来源片单已不可用，可停止分享现有快照。"));
      card.append(collectionCover(collection, false, state.browserCollections && !collection.shared), node("h3", "", collection.name || "未命名片单"), caption, collectionContents(collection), button(collection.shared ? "停止分享" : "分享给好友", collection.shared ? "button-outline" : "button-primary", async () => {
        const version = state.sessionVersion;
        if (collection.shared && !window.confirm(`停止向好友分享「${collection.name || "未命名片单"}」？`)) return;
        let body;
        if (!collection.shared && state.browserCollections) {
          const snapshot = await collectionSnapshot(collection);
          requireSession(version);
          if (!window.confirm(`将「${snapshot.name}」的 ${snapshot.items.length} 部影片保存为共享快照，供已同意的好友查看？之后片单变化不会自动同步，你可以随时停止分享。`)) return;
          body = { collection: snapshot };
        }
        await api(`/collections/${encodeURIComponent(collection.id)}/share`, { method: collection.shared ? "DELETE" : "POST", ...(body ? { body } : {}) });
        if (version !== state.sessionVersion) return;
        toast(collection.shared ? "已停止分享该片单快照。" : "片单快照已分享给好友，之后的修改不会自动同步。");
        await loadCollections();
      }));
      return card;
    }));
    if (!state.collections.length) byId("collection-list").append(emptyState("把喜欢的电影，放进片单", "还没有可分享的片单。先在 MovieClaw 中创建一份属于你的片单。", "list-video"));
  }

  function profileDraft() {
    return { nickname: byId("nickname-input").value.trim(), discoverable: byId("discoverable-input").checked, public_recent: byId("public-recent-input").checked, share_favorites: byId("share-favorites-input").checked, share_current: byId("share-current-input").checked, share_history: byId("share-history-input").checked, share_posts: byId("share-posts-input").checked };
  }

  function renderPrivacySummary() {
    if (!state.user) return;
    const draft = profileDraft();
    byId("public-recent-input").disabled = !draft.discoverable;
    byId("share-favorites-input").disabled = !draft.discoverable;
    document.querySelector(".public-options").classList.toggle("is-gated", !draft.discoverable);
    const card = node("div", "public-profile-card");
    const heading = node("div", "public-profile-heading");
    heading.append(avatar({ ...state.user, nickname: draft.nickname || displayName(state.user) }), node("h3", "", draft.nickname || "你的显示姓名"));
    card.append(heading);
    const tags = node("div", "scope-tags");
    if (draft.discoverable) {
      tags.append(node("span", "scope-tag", "公开姓名"));
      if (draft.public_recent) tags.append(node("span", "scope-tag", "近期观看 · 5 条"));
      if (draft.share_favorites) tags.append(node("span", "scope-tag", `喜欢电影 · ${state.favorites.length} 部`));
    } else tags.append(node("span", "scope-tag is-private", "未在广场展示"));
    card.append(tags);
    if (draft.discoverable && draft.share_favorites && state.favorites.length) card.append(movieShelf(state.favorites.slice(0, 3)));
    byId("public-card-preview").replaceChildren(card);
    const current = state.user;
    const rows = [
      ["广场姓名", current.discoverable],
      ["广场近期观看", current.discoverable && current.public_recent !== false],
      ["广场喜欢电影", current.discoverable && current.share_favorites],
      ["好友正在观看", current.share_current],
      ["好友观影历史", current.share_history],
      ["跨服推荐动态", current.share_posts],
    ];
    byId("sharing-scope").replaceChildren(...rows.map(([label, enabled]) => {
      const row = node("div", "scope-row");
      row.append(node("span", "", label), node("span", `scope-status${enabled ? " is-on" : ""}`, enabled ? "已开启" : "未分享"));
      return row;
    }));
  }

  // 公开预览携带服务端签发的短期 token，最终提交绑定这次看到的设置和记录，
  // 防止预览后新观影数据进入广场，或用户修改设置后误用旧的确认。
  async function previewProfile(forSave) {
    const version = state.sessionVersion;
    if (!byId("privacy-form").reportValidity()) return;
    const draft = profileDraft();
    if (!draft.nickname) throw new Error("请填写昵称。");
    window.MovieFriendsSettings.requireSaved("display-policy",version,"媒体库展示范围");
    const result = await api("/profile/preview", { method: "POST", body: draft });
    if (!state.user || version !== state.sessionVersion) return;
    if (JSON.stringify(draft) !== JSON.stringify(profileDraft())) throw new Error("设置已变更，请重新预览。");
    state.pendingPreview = forSave ? { draft, token: result.preview_token, publicProfile: result.public_profile } : null;
    const content = byId("preview-content");
    const publicSection = node("section", "preview-block");
    publicSection.append(node("h3", "", "开放广场 · 所有浏览广场的用户可见"));
    if (result.public_profile) {
      publicSection.append(node("h4", "", result.public_profile.nickname || draft.nickname));
      if (draft.public_recent) publicSection.append(node("h4", "preview-section-title", "近期观看"), historyList(result.public_profile.history, 5));
      if (draft.share_favorites) publicSection.append(node("h4", "preview-section-title", "我喜欢的电影"), list(result.public_profile.favorites).length ? movieShelf(result.public_profile.favorites) : node("p", "empty-inline", "当前喜欢列表为空。"));
      if (!draft.public_recent && !draft.share_favorites) publicSection.append(node("p", "muted", "仅公开姓名，不包含观影记录或喜欢电影。"));
    } else publicSection.append(node("p", "empty-inline", "未开启广场展示，整张个人名片都不会公开。"));
    const friendSection = node("section", "preview-block");
    friendSection.append(node("h3", "", "好友可见 · 仅双方已同意的好友"));
    appendSharedData(friendSection, result.friend_data || {});
    friendSection.append(node("p", "field-help", draft.share_posts ? "已开启跨服推荐动态：已确认的跨服好友可查看你已发布和之后发布的推荐；评论仅参与双方可见。" : "跨服推荐动态未分享。本服公开推荐由每条发布操作单独决定。"));
    content.replaceChildren(publicSection, friendSection);
    if (result.archive_included) content.prepend(node("p", "notice", "已合并 MovieClaw 与本人确认导入的 Emby/Plex 历史，按观看时间排序。确认保存后按所选范围分享，媒体库排除规则继续生效。"));
    byId("public-confirm-actions").hidden = !forSave;
    byId("preview-description").textContent = forSave ? "请核对姓名、影片和好友分享范围。确认后才保存本次分享内容。" : "这是待保存设置对应的展示内容，预览不会修改授权。";
    window.MovieFriendsUI.open(byId("preview-dialog"));
  }

  async function saveProfile(profile, extra = {}) {
    const version = state.sessionVersion;
    if(["discoverable","share_current","share_history","share_posts"].some(key=>profile[key]&&!state.user[key]))window.MovieFriendsSettings.requireSaved("display-policy",version,"媒体库展示范围");
    let result;
    try { result = await api("/profile", { method: "PATCH", body: { ...profile, ...extra } }); }
    catch (error) {
      if (version === state.sessionVersion && [400, 409].includes(error.status)) {
        state.pendingPreview = null;
        if (byId("preview-dialog").open) byId("preview-dialog").close();
      }
      throw error;
    }
    if (!state.user || version !== state.sessionVersion) return;
    state.user = { ...state.user, ...result.user };
    if (profile.discoverable && profile.share_favorites && state.pendingPreview?.publicProfile) state.publicFavorites = list(state.pendingPreview.publicProfile.favorites);
    if (!profile.discoverable || !profile.share_favorites) state.publicFavorites = [];
    window.MovieFriendsSettings.clear("sharing",state.sessionVersion);
    populateProfile();
    state.pendingPreview = null;
    if (byId("preview-dialog").open) byId("preview-dialog").close();
    toast("分享设置已保存。");
    // 撤回只依赖插件已完成的写入，不用额外查询宿主，宿主离线也应报告保存成功。
  }

  byId("retry-session").addEventListener("click", () => runAction(byId("retry-session"), startSession));
  byId("demo-toggle").addEventListener("change", () => saveDemoPreference(byId("demo-toggle").checked));

  for (const tab of document.querySelectorAll("[data-tab]")) tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  byId("refresh-button").addEventListener("click", () => runAction(byId("refresh-button"), async () => {
    const version = state.sessionVersion;
    await refreshSocial();
    if (!state.user || version !== state.sessionVersion) return;
    if (state.tab === "privacy") await Promise.all([window.MovieFriendsProfile?.loadSettings(),window.MovieFriendsDisplayPolicy?.load(false),window.MovieFriendsNetwork?.settings()]);
    if (state.tab === "directory") await loadDirectory();
    if (state.tab === "collections") await loadCollectionSpace();
    if (state.tab === "feed") await window.MovieFriendsFeed?.load();
    if (state.friendId !== null && state.tab === "friends") await loadFriendDetail(true);
    if (!state.user || version !== state.sessionVersion) return;
    toast(state.tab==="privacy"?"设置状态已刷新，未保存草稿仍保留。":"动态已更新。");
  }));

  byId("invite-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runAction(event.submitter || byId("invite-form").querySelector("button"), async () => {
      const inviteCode = byId("invite-input").value.trim();
      if (!inviteCode) return;
      if (await sendRequest({ invite_code: inviteCode })) byId("invite-input").value = "";
    });
  });

  byId("copy-invite").addEventListener("click", () => runAction(byId("copy-invite"), async () => {
    const version = state.sessionVersion;
    if (!state.user?.invite_code) throw new Error("邀请码暂不可用，请刷新后重试。");
    if (!navigator.clipboard?.writeText) throw new Error("浏览器未允许复制，请通过 HTTPS 或本机安全连接打开页面后重试。");
    await navigator.clipboard.writeText(state.user.invite_code);
    if (version !== state.sessionVersion) return;
    toast("邀请码已复制。请仅发送给你愿意添加的朋友。");
  }));

  byId("privacy-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runAction(event.submitter || byId("privacy-form").querySelector("button[type=submit]"), async () => {
      const draft = profileDraft();
      if (!draft.nickname) throw new Error("请填写昵称。");
      if (draft.discoverable || draft.share_history) await previewProfile(true); else await saveProfile(draft);
    });
  });
  byId("privacy-form").addEventListener("input", renderPrivacySummary);
  byId("preview-button").addEventListener("click", () => runAction(byId("preview-button"), () => previewProfile(false)));
  byId("confirm-public").addEventListener("click", () => runAction(byId("confirm-public"), async () => {
    const pending = state.pendingPreview;
    if (!pending?.token || JSON.stringify(pending.draft) !== JSON.stringify(profileDraft())) {
      state.pendingPreview = null;
      byId("preview-dialog").close();
      throw new Error("预览已失效或设置已变更，请重新预览后确认。");
    }
    await saveProfile(pending.draft, { public_consent: true, preview_token: pending.token });
  }));
  for (const id of ["close-preview", "cancel-public"]) byId(id).addEventListener("click", () => { state.pendingPreview = null; byId("preview-dialog").close(); });
  byId("preview-dialog").addEventListener("close", () => { state.pendingPreview = null; });
  byId("address-checkbox").addEventListener("change", () => { byId("confirm-address").disabled = !byId("address-checkbox").checked; });
  byId("cancel-address").addEventListener("click", () => finishAddressConsent(false));
  byId("confirm-address").addEventListener("click", () => { if (byId("address-checkbox").checked) finishAddressConsent(true); });
  byId("address-dialog").addEventListener("cancel", (event) => { event.preventDefault(); finishAddressConsent(false); });
  byId("close-media").addEventListener("click", () => byId("media-dialog").close());
  byId("media-dialog").addEventListener("close", () => { state.mediaVersion += 1; });
  byId("open-list-creator").addEventListener("click", openListCreator);
  byId("edit-favorites").addEventListener("click", () => runAction(byId("edit-favorites"), () => openFavoritesEditor()));
  byId("favorites-publish").addEventListener("click", () => switchTab("privacy"));
  byId("close-list-target").addEventListener("click", () => byId("list-target-dialog").close());
  for (const id of ["close-list-creator", "cancel-list-creator"]) byId(id).addEventListener("click", () => byId("list-create-dialog").close());
  byId("list-search-button").addEventListener("click", () => runAction(byId("list-search-button"), searchListTitles));
  byId("list-search-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); byId("list-search-button").click(); }
  });
  byId("list-create-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runAction(event.submitter || byId("list-create-form").querySelector("button[type=submit]"), async () => {
      const version = state.sessionVersion;
      const editorVersion = state.editorVersion;
      const mode = state.editorMode;
      if (mode === "favorites") {
        const result = await api("/favorites", { method: "PUT", body: { items: state.favoriteDraft } });
        requireSession(version);
        state.favorites = list(result.items);
        state.publicFavorites = list(result.public_items);
        if (editorVersion === state.editorVersion) state.favoriteDraft = [];
        state.pendingPreview = null;
        if (byId("preview-dialog").open) byId("preview-dialog").close();
        renderFavorites(); renderPrivacySummary();
        if (editorVersion === state.editorVersion) {
          byId("list-create-dialog").close();
          switchTab("collections", false);
        }
        toast(state.user.discoverable && state.user.share_favorites ? "私人列表已保存，广场仍是上次确认内容。请重新预览并发布。" : "喜欢已私人保存，尚未公开。可去设置预览确认。");
        return;
      }
      const name = byId("list-name").value.trim();
      if (!name) throw new Error("请填写片单名称。");
      const editing = state.editingList;
      await api(editing ? `/lists/${encodeURIComponent(editing.id)}` : "/lists", { method: editing ? "PUT" : "POST", body: { name, description: byId("list-description").value.trim(), items: state.listDraft, cover:state.listCover, ...(editing ? { revision: editing.revision } : {}) } });
      requireSession(version);
      if (editorVersion === state.editorVersion) {
        state.listDraft = []; state.listCover=null;
        state.editingList = null;
        byId("list-create-form").reset();
        byId("list-search-results").replaceChildren();
        byId("list-create-dialog").close();
        switchTab("collections", false);
      }
      toast(editing ? "私人片单已更新，共享快照保持原样。" : "私有片单已创建，尚未分享给任何好友。");
      await loadLists();
    });
  });

  // 演示播放使用服务端快照时间和明确的播放倍率，只更新进度节点，不重建卡片或打断对话框。
  setInterval(() => {
    if (!state.user || document.hidden) return;
    for (const card of document.querySelectorAll(".now-card[data-demo-clock]")) {
      const clock = card.demoClock;
      if (!clock) continue;
      const position = Math.min(clock.duration, clock.position + Math.max(0, Date.now() - clock.observed) * clock.rate);
      const progress = Math.max(0, Math.min(100, position / clock.duration * 100));
      clock.fill.style.width = `${progress}%`;
      clock.track.setAttribute("aria-valuenow", String(Math.round(progress)));
      clock.caption.firstChild.textContent = `${durationText(position)} / ${durationText(clock.duration)}`;
      clock.caption.lastChild.textContent = `${Math.round(progress)}%`;
    }
  }, 1000);

  // 轮询更新好友、申请及当前打开的详情或广场，不覆盖正在填写的隐私表单。
  // 各读侧独立刷新，避免好友接口失败时，广场持续展示已经撤回的公开内容。
  setInterval(async () => {
    if (!state.user || document.hidden || document.querySelector("dialog[open]")) return;
    const version = state.sessionVersion;
    const tasks = [refreshSocial(), window.MovieFriendsCommunity?.refreshUnread()];
    if (state.tab === "directory") tasks.push(loadDirectory());
    if (state.tab === "friends" && state.friendId !== null) tasks.push(loadFriendDetail(true));
    const results = await Promise.allSettled(tasks);
    const failed = results.find((result) => result.status === "rejected");
    if (failed && state.user && version === state.sessionVersion && !failed.reason.staleSession) toast(safeError(failed.reason), true);
  }, 30000);

  async function bootstrap() {
    try {
      await startSession();
    } catch (error) {
      if (error.staleSession) return;
      if (error.status === 401) { showLoginRequired(); return; }
      if (state.user) { toast(safeError(error), true); return; }
      byId("startup-state").replaceChildren(node("span", "", safeError(error)), button("重试", "button-outline", bootstrap));
    }
  }
  function openPerson(user, context = {}) { return window.MovieFriendsCommunity?.openProfile(user, context); }
  window.MovieFriendsDisplayPolicy?.configure({ api, hostApi, user: () => state.user,
    sessionVersion: () => state.sessionVersion, toast, safeError,
    async onSaved() { state.pendingPreview = null; if (byId("preview-dialog").open) byId("preview-dialog").close(); await refreshSocial(); },
  });
  window.MovieFriendsProfile?.configure({ api, hostApi, avatar, mediaPoster, mediaTitle, mediaMeta, dateText, toast, safeError,
    user: () => state.user, sessionVersion: () => state.sessionVersion,
    onAvatarUpdated(data) { if (state.user) { state.user = { ...state.user, avatar_data: data, avatar_url: null, avatar_id: "initials" }; } },
    editPrivacy() { switchTab("privacy"); },
  });
  window.MovieFriendsAvailability?.configure({ hostApi, user: () => state.user, sessionVersion: () => state.sessionVersion, safeError });
  window.MovieFriendsCommunity?.configure({
    api, avatar, presence, historyList, historyShelf, movieShelf, mediaPoster, mediaTitle, mediaMeta, dateText, mediaArtwork, releaseArtwork, toast, safeError,
    user: () => state.user, demo: () => state.demo, sessionVersion: () => state.sessionVersion,
    onProfileUpdated(profile) { if (state.user) { state.user = { ...state.user, avatar_id: profile.avatar_id, bio: profile.bio }; renderPrivacySummary(); } },
    async onBlocked() { await refreshSocial(); if (state.tab === "directory") await loadDirectory(); if (state.tab === "feed") await window.MovieFriendsFeed?.load(); },
    async showRequests() { const version = state.sessionVersion; switchTab("friends"); await refreshSocial(); requireSession(version); byId("requests-panel").open = true; byId("requests-panel").scrollIntoView({ block: "start", behavior: "smooth" }); },
    async focusPost(id) { switchTab("feed", false); await window.MovieFriendsFeed?.focusPost(id); },
    editProfile() { switchTab("privacy"); },
  });
  window.MovieFriendsNetwork?.configure({api,mediaPoster,mediaTitle,showMedia,dateText,toast,safeError,user:()=>state.user,sessionVersion:()=>state.sessionVersion,settings(){switchTab("privacy");byId("network-settings").scrollIntoView({block:"start"});}});
  window.MovieFriendsArtwork?.configure({hostApi,titleDetails,fullArtwork,toast,safeError,user:()=>state.user,sessionVersion:()=>state.sessionVersion});
  window.MovieFriendsFeed?.configure({
    api, hostApi, titleDetails, avatar, mediaPoster, mediaArtwork, releaseArtwork, mediaTitle, showMedia, toast, safeError,
    user: () => state.user, demo: () => state.demo, sessionVersion: () => state.sessionVersion,
    addToList, onFavoritesChanged: loadFavorites, friends: () => state.friends, openProfile: openPerson,
    blockUser: (user, post) => window.MovieFriendsCommunity?.blockUser(user, post), reportPost: (post) => window.MovieFriendsCommunity?.reportPost(post), reportComment: (comment) => window.MovieFriendsCommunity?.reportComment(comment),
  });
  bootstrap();
})();

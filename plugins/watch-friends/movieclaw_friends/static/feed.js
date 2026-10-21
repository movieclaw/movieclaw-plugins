"use strict";

// 推荐动态独立管理分页与编辑状态；身份、影片展示和请求边界复用主页面的窄接口。
// 所有用户文字都作为文本写入，演示互动只留在内存，不冒充真实成员发布内容。
window.MovieFriendsFeed = (() => {
  let bridge;
  let generation = 0;
  let searchVersion = 0;
  let eligibilityVersion = 0;
  let artworkVersion = 0;
  let posts = [];
  let cursor = null;
  let selected = null;
  let demoSignature = "";
  let favoriteQueue = Promise.resolve();
  let scope = "local";
  let friendId = "";
  const demos = new Map();
  const el = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  };
  const list = (value) => Array.isArray(value) ? value : [];
  const byId = (id) => document.getElementById(id);
  const valid = (version, session) => version === generation && session === bridge.sessionVersion() && bridge.user();
  const stamp = (value) => {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date) : "刚刚";
  };

  function action(label, handler, className = "button-quiet") {
    const button = window.MovieFriendsIcons.label(el("button", `button ${className}`), label);
    button.type = "button";
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const session = bridge.sessionVersion();
      button.disabled = true;
      try { await handler(button); }
      catch (error) { if (session === bridge.sessionVersion() && !error.staleSession) bridge.toast(bridge.safeError(error), true); }
      finally { button.disabled = false; }
    });
    return button;
  }

  function normalizeTitle(item) {
    const id = Number(item.external_id ?? item.tmdb_id);
    const kind = item.media_type || item.kind;
    if (!Number.isSafeInteger(id) || id <= 0 || !["movie", "tv"].includes(kind)) return null;
    const poster = typeof item.poster_url === "string" && item.poster_url.match(/^https:\/\/image\.tmdb\.org\/t\/p\/(?:w[1-9]\d*|original)(\/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg|png|webp))$/i);
    return { tmdb_id: id, kind, title: String(item.title || "未命名影片").slice(0, 300), year: Number.isInteger(item.release_year) ? item.release_year : item.year || null, poster_path: poster ? poster[1] : null };
  }

  async function checkSelection(item, hint) {
    const version=++eligibilityVersion,session=bridge.sessionVersion(),submit=byId("feed-compose-form").querySelector("button[type=submit]");
    byId("feed-selected").querySelector(".feed-provenance-retry")?.remove();hint.textContent="正在核验来源…";submit.disabled=true;
    try {
      const result=await bridge.api("/feed/eligibility",{method:"POST",body:{media:item}});
      if(version!==eligibilityVersion||session!==bridge.sessionVersion()||selected!==item)return;
      hint.textContent=result.allowed?"已选择 · 可公开推荐":result.message;hint.dataset.status=result.status;submit.disabled=!result.allowed;
      if(!result.allowed&&result.status==="unverified")retry();
    }catch(error){
      if(version!==eligibilityVersion||session!==bridge.sessionVersion()||selected!==item)return;
      hint.textContent=bridge.safeError(error);hint.dataset.status="unverified";retry();
    }
    function retry(){const button=action("重试核验",()=>checkSelection(item,hint));button.classList.add("feed-provenance-retry");byId("feed-selected").append(button);}
  }

  function chooseFilm(item) {
    selected=item;
    const preview=byId("feed-selected"),hint=el("small","muted feed-share-status","已选择");preview.hidden=false;
    preview.replaceChildren(bridge.mediaPoster(item,false,false),el("strong","",`${item.title}${item.year?` · ${item.year}`:""}`),hint);
    checkSelection(item,hint);loadCovers(item);
  }

  async function loadCovers(item) {
    const root=byId("feed-covers");root.coverPicker?.destroy();root.hidden=false;
    root.coverPicker=window.MovieFriendsArtwork.picker(root,item,{selected:window.MovieFriendsArtwork.selections(item),onChange(images){
      item.cover_images=images;
      if(images.length){item.cover_path=images[0].path;item.cover_type=images[0].type;}else{delete item.cover_images;delete item.cover_path;delete item.cover_type;}
    }});
  }

  // 星形只作为视觉图案，评分选择使用原生单选框，支持键盘与读屏操作。
  function starIcon() { return window.MovieFriendsIcons.create("star"); }

  function selectedRating() { return Number(byId("feed-rating")?.querySelector("input:checked")?.value) || null; }
  function updateRating() {
    const rating = selectedRating();
    byId("feed-rating").querySelectorAll(".feed-rating-option").forEach(label => label.classList.toggle("is-filled", Number(label.querySelector("input").value) <= rating));
    byId("feed-rating-value").textContent = rating ? `${rating} / 5 星` : "尚未评分";
    byId("feed-body").required = !rating;
  }
  function clearRating() { byId("feed-rating").querySelectorAll("input").forEach(input => { input.checked = false; }); updateRating(); }

  function ensureRoot() {
    const root = byId("feed-root");
    if (!root || root.children.length) return;
    const intro = el("div", "section-intro");
    const title=el("h2","","好故事，留给同频的人");title.append(window.MovieFriendsHelp.create("影友动态","推荐喜欢的电影或剧集，与影友点赞、评论，也可以收藏到自己的片单。"));intro.append(el("p","eyebrow","THE FILMS THAT STAY WITH US"),title);
    const compose = el("details", "feed-compose panel");
    compose.append(window.MovieFriendsIcons.label(el("summary"), "＋ 分享一部好电影"));
    const form = el("form", "feed-compose-form");
    form.id = "feed-compose-form";
    const searchLabel = el("label", "", "选择要推荐的影片"); searchLabel.htmlFor = "feed-search";
    const searchRow = el("div", "input-action");
    const input = el("input"); input.id = "feed-search"; input.placeholder = "搜索电影或剧集"; input.maxLength = 100; input.autocomplete = "off";
    searchRow.append(input, action("搜索影片", searchTitles, "button-outline"));
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); searchTitles().catch((error) => bridge.toast(bridge.safeError(error), true)); } });
    const results = el("div", "feed-search-results"); results.id = "feed-search-results"; results.setAttribute("aria-live", "polite");
    const choice = el("div", "feed-selected"); choice.id = "feed-selected"; choice.hidden = true;
    const covers=el("section","feed-cover-picker");covers.id="feed-covers";covers.hidden=true;
    const rating = el("fieldset", "feed-rating"); rating.id = "feed-rating"; rating.append(el("legend", "", "你的评分（可选）"));
    const stars = el("div", "feed-rating-stars");
    for (let score = 1; score <= 5; score++) {
      const label = el("label", "feed-rating-option"); label.title = `${score} 星`;
      const radio = el("input"); radio.type = "radio"; radio.name = "feed-rating"; radio.value = String(score); radio.setAttribute("aria-label", `${score} 星`); radio.addEventListener("change", updateRating);
      label.append(radio, starIcon()); stars.append(label);
    }
    const ratingValue = el("span", "field-help", "尚未评分"); ratingValue.id = "feed-rating-value"; ratingValue.setAttribute("aria-live", "polite");
    rating.append(stars, ratingValue, action("清除评分", clearRating));
    const bodyLabel = el("label", "", "你的推荐理由（打星后可不填）"); bodyLabel.htmlFor = "feed-body";
    const body = el("textarea"); body.id = "feed-body"; body.maxLength = 2000; body.rows = 3; body.required = true; body.placeholder = "打星或写下推荐理由，任选一种。也可以聊聊让你难忘的那一幕。";
    const notice = el("p", "field-help", "发布后，本服登录用户可看到你的昵称、评分、推荐文字和所选影片；不会附带私人观影记录或服务器地址。删除推荐会一并删除该帖的点赞和评论。");
    const submit = el("button", "button button-primary", "发布公开推荐"); submit.type = "submit";
    form.append(searchLabel, searchRow, results, choice, covers, rating, bodyLabel, body, notice, submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!selected) { bridge.toast("请先搜索并选择一部影片。", true); return; }
      const score = selectedRating();
      if (!score && !body.value.trim()) { bridge.toast("请打星或填写推荐理由。", true); return; }
      const session = bridge.sessionVersion();
      submit.disabled = true;
      try {
        await bridge.api("/feed", { method: "POST", body: { media: selected, body: body.value.trim(), rating: score, publish_consent: true } });
        if (session !== bridge.sessionVersion()) return;
        body.value = ""; input.value = ""; selected = null; eligibilityVersion++; artworkVersion++;bridge.releaseArtwork(covers);covers.replaceChildren();covers.hidden=true; choice.replaceChildren(); choice.hidden = true; results.replaceChildren();
        clearRating();
        compose.open = false;
        bridge.toast("推荐已发布。只有你主动发布的内容会显示在动态中。");
        await load();
      } catch (error) { if (session === bridge.sessionVersion() && !error.staleSession) bridge.toast(bridge.safeError(error), true); }
      finally { submit.disabled = false; }
    });
    compose.append(form);
    const toolbar = el("div", "section-heading feed-toolbar");
    const filters = el("div", "feed-filters");
    const scopeSelect = el("select"); scopeSelect.id = "feed-scope"; scopeSelect.setAttribute("aria-label", "动态范围");
    for (const [value, label] of [["local", "本服影友"], ["friends", "跨服好友"]]) { const option = el("option", "", label); option.value = value; scopeSelect.append(option); }
    const friendSelect = el("select"); friendSelect.id = "feed-friend"; friendSelect.setAttribute("aria-label", "选择已确认的跨服好友"); friendSelect.hidden = true;
    scopeSelect.addEventListener("change", () => { scope = scopeSelect.value; posts = []; cursor = null; syncScope(); render(); load(); });
    friendSelect.addEventListener("change", () => { friendId = friendSelect.value; posts = []; cursor = null; render(); load(); });
    filters.append(scopeSelect, friendSelect);
    toolbar.append(filters, action("刷新动态", () => load(), "button-outline"));
    const scopeNotice = el("p", "field-help"); scopeNotice.id = "feed-scope-notice"; scopeNotice.hidden = true;
    const status = el("p", "notice"); status.id = "feed-status"; status.hidden = true; status.setAttribute("role", "status");
    const grid = el("div", "feed-grid"); grid.id = "feed-posts"; grid.setAttribute("aria-live", "polite");
    const more = action("加载更多推荐", () => load(true), "button-outline"); more.id = "feed-more"; more.hidden = true;
    const layout = el("div", "feed-layout");
    const stream = el("div", "feed-stream"); stream.append(compose, toolbar, scopeNotice, status, grid, more);
    const aside = el("aside", "feed-aside"); aside.id = "feed-aside"; aside.setAttribute("aria-label", "观影名片与影友");
    layout.append(stream, aside); root.append(intro, layout);
    syncScope();
  }

  function syncScope() {
    if (!byId("feed-scope")) return;
    const friends = list(bridge.friends?.()).filter((friend) => friend.is_remote && !friend.is_demo && (!friend.status || friend.status === "accepted"));
    if (!friends.some((friend) => String(friend.id) === friendId)) friendId = friends.length ? String(friends[0].id) : "";
    const select = byId("feed-friend");
    select.replaceChildren(...friends.map((friend) => { const option = el("option", "", friend.nickname); option.value = String(friend.id); return option; }));
    if (!friends.length) { const empty = el("option", "", "尚无已确认的跨服好友"); empty.value = ""; select.append(empty); }
    select.value = friendId; select.hidden = scope !== "friends";
    byId("feed-scope").value = scope;
    byId("feed-root").querySelector(".feed-compose").hidden = scope !== "local";
    byId("feed-scope-notice").hidden = scope !== "friends";
    byId("feed-scope-notice").replaceChildren(window.MovieFriendsHelp.create("跨服动态范围","仅查看对方单独授权的推荐。跨服评论仅你与帖作者双方可见，撤回授权后无法继续访问。"));
  }

  function postBase(post) {
    return post.relationship_id ? `/friends/${encodeURIComponent(post.relationship_id)}/feed/${encodeURIComponent(post.id)}` : `/feed/${encodeURIComponent(post.id)}`;
  }

  function personLink(user, content, post) {
    const link = action("", () => bridge.openProfile?.(user.is_self ? bridge.user() : user, user.is_self ? {} : post), "person-link");
    link.append(content);
    link.setAttribute("aria-label", `查看${user.nickname || "影友"}的个人主页`);
    return link;
  }

  async function searchTitles() {
    const query = byId("feed-search").value.trim();
    if (!query) { bridge.toast("请输入影片名称。", true); return; }
    const version = ++searchVersion;
    const session = bridge.sessionVersion();
    const target = byId("feed-search-results");
    target.replaceChildren(el("p", "muted", "正在搜索影片…"));
    try {
      const result = await bridge.hostApi("/search/titles", { method: "POST", body: { query, provider: "tmdb", save_history: false } });
      if (version !== searchVersion || session !== bridge.sessionVersion()) return;
      const items = list(result.titles).map(normalizeTitle).filter(Boolean).slice(0, 12);
      target.replaceChildren(...items.map((item) => {
        const choice = action("", () => {
          if (selected?.tmdb_id !== item.tmdb_id || selected?.kind !== item.kind) clearRating();
          chooseFilm(item);target.replaceChildren();
        }, "feed-search-choice");
        choice.append(bridge.mediaPoster(item, false, false), el("span", "", `${item.title}${item.year ? ` · ${item.year}` : ""}`));
        return choice;
      }));
      if (!items.length) target.append(el("p", "muted", "没有找到匹配的影片，请换个名称。"));
    } catch (error) {
      if (version === searchVersion && session === bridge.sessionVersion()) target.replaceChildren(el("p", "muted", bridge.safeError(error)));
    }
  }

  function demoPosts() {
    const demo = bridge.demo();
    if (!demo?.enabled || scope !== "local") return [];
    return list(demo.users).flatMap((entry, personIndex) => list(entry.data?.recommendations).map((post, index) => {
      if (!demos.has(post.id)) demos.set(post.id, { liked: false, comments: [] });
      const local = demos.get(post.id);
      return { ...post, author: entry.user, is_demo: true, liked: local.liked, like_count: 8 + personIndex * 5 + index + Number(local.liked), comment_count: local.comments.length, comments: local.comments };
    }));
  }

  function nativeLink(label, href) {
    const link = window.MovieFriendsIcons.label(el("a", "button button-quiet"), label);
    link.href = href; link.target = "_top";
    return link;
  }

  function saveFavorite(item) {
    const session = bridge.sessionVersion();
    // 同时收藏两张卡片时按顺序重新读取本人列表，避免后一次整表保存覆盖前一次。
    const pending = favoriteQueue.catch(() => {}).then(async () => {
      if (session !== bridge.sessionVersion() || !bridge.user()) return;
      const result = await bridge.api("/favorites");
      const items = list(result.items);
      if (items.some((film) => film.tmdb_id === item.tmdb_id && film.kind === item.kind)) { bridge.toast("这部影片已经在你的喜欢列表中。"); return; }
      await bridge.api("/favorites", { method: "PUT", body: { items: [...items, item] } });
      await bridge.onFavoritesChanged?.();
      bridge.toast("已加入我喜欢的电影。广场公开内容需在设置中预览确认。");
    });
    favoriteQueue = pending;
    return pending;
  }

  function commentRow(comment, post) {
    const row = el("div", "feed-comment");
    const portrait = personLink(comment.author, bridge.avatar(comment.author), post); portrait.classList.add("comment-avatar-link");
    const name = personLink(comment.author, el("strong", "", comment.author.nickname), post); name.classList.add("comment-name-link");
    row.append(portrait, name, el("span", "comment-body", comment.body));
    if (comment.author.is_self) row.append(action("删除评论", async () => {
      if (post.is_demo) {
        const local = demos.get(post.id);
        local.comments = local.comments.filter((item) => item.id !== comment.id);
      } else {
        await bridge.api(post.relationship_id ? `${postBase(post)}/comments/${encodeURIComponent(comment.id)}` : typeof comment.id==="string"&&comment.id.startsWith("n")?`/feed/network-comments/${encodeURIComponent(comment.id.slice(1))}`:`/feed/comments/${encodeURIComponent(comment.id)}`, { method: "DELETE" });
        post.comments = post.comments.filter((item) => item.id !== comment.id); post.comment_count -= 1;
      }
      const discussion = row.closest(".feed-discussion");
      row.remove();
      if (discussion) discussion.querySelector("summary").textContent = `评论 ${post.is_demo ? demos.get(post.id).comments.length : post.comment_count}`;
    }));
    if (!comment.author.is_self && !post.is_demo && !post.relationship_id) {
      const report = action("举报评论", () => bridge.reportComment?.(comment), "comment-report button-quiet");
      row.append(report);
    }
    return row;
  }

  function card(post) {
    const article = el("article", "panel feed-card");
    article.dataset.postId = String(post.id);
    article.dataset.demo = String(Boolean(post.is_demo));
    const header = el("div", "feed-author");
    const author = el("div", "person-copy");
    const name = el("strong", "", post.author.nickname);
    if (post.is_demo) name.append(el("span", "demo-badge", "演示"));
    author.append(personLink(post.author, name, post), el("small", "muted", stamp(post.created_at)));
    header.append(personLink(post.author, bridge.avatar(post.author), post), author);
    if (post.author.is_self && !post.is_demo) header.append(action("删除推荐", async () => {
      if (!window.confirm("删除这条推荐及其全部点赞和评论？")) return;
      await bridge.api(postBase(post), { method: "DELETE" });
      posts = posts.filter((item) => item !== post); render();
    }));
    if (!post.author.is_self && !post.is_demo) {
      const menu = el("details", "feed-menu");
      const summary = el("summary"); summary.append(window.MovieFriendsIcons.create("ellipsis")); summary.setAttribute("aria-label", "推荐操作");
      const choices = el("div", "feed-menu-items");
      choices.append(action("屏蔽这位影友", async () => { await bridge.blockUser?.(post.author, post); await load(); }));
      if (!post.relationship_id) choices.append(action("举报这条推荐", () => bridge.reportPost?.(post)));
      menu.append(summary, choices); header.append(menu);
    }
    const film = el("div", "feed-film");
    const title = el("div", "feed-film-copy");
    const heading = el("h3"); heading.append(bridge.mediaTitle(post.media, post.is_demo));
    title.append(el("span", "small-label", post.media.kind === "tv" ? "剧集推荐" : "电影推荐"), heading, el("small", "muted", post.media.year || ""));
    const selectedImages=window.MovieFriendsArtwork.selections(post.media);
    if(selectedImages.length){film.classList.add("has-gallery");film.append(window.MovieFriendsArtwork.gallery(selectedImages),title);}else film.append(bridge.mediaArtwork(post.media, post.is_demo), title);
    const body = el("p", "feed-post-copy", post.body);
    const rating = el("div", "feed-post-rating");
    if (Number.isInteger(post.rating) && post.rating >= 1 && post.rating <= 5) {
      rating.setAttribute("role", "img"); rating.setAttribute("aria-label", `评分：${post.rating} / 5 星`);
      for (let score = 1; score <= 5; score++) { const star = starIcon(); if (score <= post.rating) star.classList.add("is-filled"); rating.append(star); }
      const value = el("span", "", `${post.rating} / 5`); value.setAttribute("aria-hidden", "true"); rating.append(value);
    }
    const actions = el("div", "feed-reactions");
    const like = action(`${post.liked ? "♥ 已赞" : "♡ 点赞"} ${post.like_count}`, async (button) => {
      const liked = !post.liked;
      if (post.is_demo) { demos.get(post.id).liked = liked; post.like_count += liked ? 1 : -1; post.liked = liked; }
      else Object.assign(post, await bridge.api(`${postBase(post)}/like`, { method: "PUT", body: { liked } }));
      window.MovieFriendsIcons.label(button,`${post.liked ? "♥ 已赞" : "♡ 点赞"} ${post.like_count}`);
      button.setAttribute("aria-pressed", String(post.liked));
    });
    like.setAttribute("aria-pressed", String(post.liked));
    actions.append(like, action("♡ 收藏影片", () => saveFavorite(post.media)), action("＋ 加入片单", () => bridge.addToList(post.media)));
    const links = el("div", "feed-native-actions");
    const query = encodeURIComponent(post.media.title);
    const id = Number(post.media.tmdb_id);
    if (Number.isSafeInteger(id) && id > 0 && ["movie", "tv"].includes(post.media.kind)) links.append(nativeLink("影片详情 ↗", `/media/${post.media.kind}/${id}`));
    links.append(nativeLink("搜索影片 ↗", `/search?q=${query}&tab=media&private=1`), nativeLink("查找观看 ↗", `/search?q=${query}&tab=library&private=1`), nativeLink("找资源下载 ↗", `/search?q=${query}&private=1`));
    const discussion = el("details", "feed-discussion");
    discussion.append(el("summary", "", `评论 ${post.comment_count}`));
    const comments = el("div", "feed-comments");
    comments.append(...list(post.comments).map((comment) => commentRow(comment, post)));
    if (!post.is_demo && post.comment_count > post.comments.length) comments.append(action("查看全部评论", async (button) => {
      const result = await bridge.api(`${postBase(post)}/comments?limit=30`);
      showCommentPage(result, false);
      button.remove();
    }));
    function showCommentPage(result, append) {
      if (!append) comments.replaceChildren();
      comments.append(...list(result.comments).map((comment) => commentRow(comment, post)));
      if (result.next_before) comments.append(action("更早的评论", async (button) => {
        const next = await bridge.api(`${postBase(post)}/comments?limit=30&before=${encodeURIComponent(result.next_before)}`);
        button.remove(); showCommentPage(next, true);
      }));
    }
    const form = el("form", "feed-comment-form");
    const input = el("input"); input.placeholder = post.is_demo ? "写一条演示评论" : "聊聊你的看法"; input.maxLength = 500; input.required = true; input.setAttribute("aria-label", `评论${post.author.nickname}的推荐`);
    const submit = el("button", "button button-outline", "发送评论"); submit.type = "submit";
    const publicReply=el("input");publicReply.type="checkbox";let commentRequest=null;
    form.append(input, submit);if(post.network_public&&!post.relationship_id&&!post.is_demo){const label=el("label","feed-network-reply");label.append(publicReply,el("span","","同时公开到联网讨论（昵称、头像、评论）"));form.append(label);}
    input.addEventListener("input",()=>{commentRequest=null;});
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value.trim(); if (!text) return;
      submit.disabled = true;
      try {
        let comment;
        if (post.is_demo) {
          comment = { id: `preview-${Date.now()}`, author: { ...bridge.user(), is_self: true }, body: text, created_at: new Date().toISOString() };
          demos.get(post.id).comments.push(comment);
        } else {
          commentRequest=commentRequest||crypto.randomUUID();
          comment = await bridge.api(publicReply.checked?`/feed/${post.id}/network-comments`:`${postBase(post)}/comments`, { method: "POST", body: { body: text, ...(publicReply.checked?{consent:true,request_id:commentRequest}:post.relationship_id ? { request_id: commentRequest } : {}) } });commentRequest=null;
          post.comments.push(comment); post.comment_count += 1;
        }
        comments.append(commentRow(comment, post)); input.value = "";
        discussion.querySelector("summary").textContent = `评论 ${post.is_demo ? demos.get(post.id).comments.length : post.comment_count}`;
      } catch (error) { if (!error.staleSession) bridge.toast(bridge.safeError(error), true); }
      finally { submit.disabled = false; }
    });
    discussion.append(comments, form);
    if (post.is_demo) discussion.append(el("small", "field-help", "演示互动仅保留在本次页面，不会发布给真实用户。"));
    const tools = el("details", "feed-tools"); tools.append(el("summary", "", "片源与更多操作"), links);
    const availability = window.MovieFriendsAvailability?.create(post.media);
    if (availability) tools.append(availability);
    const footer = el("div", "feed-card-footer"); footer.append(discussion, tools);
    article.append(header, film); if (rating.children.length) article.append(rating); if (post.body) article.append(body); article.append(actions, footer);
    return article;
  }

  function renderAside(visible) {
    const aside = byId("feed-aside"); if (!aside) return;
    const user = bridge.user();
    const own = el("section", "feed-identity panel");
    const portrait = personLink({ ...user, is_self: true }, bridge.avatar(user), {});
    own.append(el("p", "eyebrow", "YOUR CINEMA CORNER"), portrait, el("h3", "", user.nickname), el("p", "muted", user.bio || "把喜欢的故事，分享给同频的人。"));
    own.append(action("查看我的主页 ↗", () => bridge.openProfile?.({ ...user, is_self: true }), "button-outline"));
    const circle = el("section", "feed-circle panel"); circle.append(el("p", "eyebrow", "PEOPLE & STORIES"), el("h3", "", "在这里，遇见影友"));
    const authors = new Map();
    for (const post of visible) {
      if (!post.author.is_self && !authors.has(post.author.id)) authors.set(post.author.id, post);
    }
    for (const post of [...authors.values()].slice(0, 6)) {
      const row = personLink(post.author, bridge.avatar(post.author), post); row.classList.add("feed-circle-person");
      const copy = el("span", "person-copy"); copy.append(el("strong", "", post.author.nickname));
      if (post.is_demo) copy.append(el("small", "muted", "虚拟演示影友"));
      else copy.append(el("small", "muted", post.relationship_id ? "跨服好友" : "推荐了一部电影"));
      row.append(copy, window.MovieFriendsIcons.create("arrow-up-right","muted")); circle.append(row);
    }
    if (!authors.size) circle.append(el("p", "field-help", "有影友发布推荐后，会在这里与你相遇。"));
    aside.replaceChildren(own, circle);
  }

  function render() {
    if (!bridge.user()) return;
    ensureRoot();
    const visible = [...posts, ...demoPosts()];
    bridge.releaseArtwork(byId("feed-posts"));
    byId("feed-posts").replaceChildren(...visible.map(card));
    renderAside(visible);
    if (!visible.length) byId("feed-posts").append(el("p", "empty-state feed-empty", scope === "friends" ? "这里显示已确认跨服好友主动分享的推荐。请选择好友或稍后刷新。" : "还没有人发布推荐。分享第一部你喜欢的电影吧。"));
    byId("feed-more").hidden = cursor == null;
    demoSignature = currentDemoSignature();
  }

  function currentDemoSignature() {
    const demo = bridge.demo();
    return demo?.enabled ? JSON.stringify(list(demo.users).flatMap((entry) => list(entry.data?.recommendations).map((post) => [post.id, post.media?.tmdb_id, post.body]))) : "off";
  }

  async function load(append = false) {
    if (!bridge?.user()) return;
    ensureRoot();
    syncScope();
    const version = ++generation;
    const session = bridge.sessionVersion();
    const status = byId("feed-status");
    if (scope === "friends" && !friendId) { posts = []; cursor = null; status.hidden = true; render(); return; }
    status.hidden = false; status.textContent = "正在读取影友推荐…";
    try {
      const relation = scope === "friends" ? friendId : null;
      const base = relation ? `/friends/${encodeURIComponent(relation)}/feed` : "/feed";
      const result = await bridge.api(`${base}?limit=20${append && cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`);
      if (!valid(version, session)) return;
      const received = list(result.posts).map((post) => relation ? { ...post, relationship_id: relation } : post);
      posts = append ? [...posts, ...received.filter((item) => !posts.some((old) => old.id === item.id))] : received;
      cursor = result.next_before;
      status.hidden = true; render();
    } catch (error) {
      if (!valid(version, session) || error.staleSession) return;
      if (scope === "friends") { posts = []; cursor = null; }
      status.textContent = bridge.safeError(error); render();
    }
  }

  function reset() {
    generation += 1; searchVersion += 1; artworkVersion += 1; posts = []; cursor = null; selected = null; scope = "local"; friendId = ""; demoSignature = ""; favoriteQueue = Promise.resolve(); demos.clear();
    bridge?.releaseArtwork(byId("feed-root")); byId("feed-root")?.replaceChildren();
  }

  return {
    configure(value) { bridge = value; }, load, reset,
    async focusPost(id) {
      const startedSession = bridge.sessionVersion();
      scope = "local"; ensureRoot(); syncScope();
      await load();
      if (startedSession !== bridge.sessionVersion() || !bridge.user()) return;
      const session = startedSession; const version = generation;
      if (!bridge.user()) return;
      if (!posts.some((post) => String(post.id) === String(id))) {
        const post = await bridge.api(`/feed/${encodeURIComponent(id)}`);
        if (!valid(version, session)) return;
        posts.unshift(post); render();
      }
      const target = Array.from(byId("feed-posts").children).find((card) => card.dataset.postId === String(id));
      if (target) { target.classList.add("is-focused"); target.querySelector(".feed-discussion").open = true; target.scrollIntoView({ block: "center", behavior: "smooth" }); }
    },
    recommend(item) {
      if (!bridge?.user()) return;
      ensureRoot();
      scope = "local"; syncScope();
      const value = normalizeTitle(item);
      if (!value) { bridge.toast("这部影片缺少可靠的作品编号，请先搜索选择。", true); return; }
      const path = typeof item.poster_path === "string" && /^\/[A-Za-z0-9_-]{1,200}\.(jpg|jpeg|png|webp)$/i.test(item.poster_path) ? item.poster_path : value.poster_path;
      if (selected?.tmdb_id !== value.tmdb_id || selected?.kind !== value.kind) clearRating();
      chooseFilm({ ...value, poster_path: path });
      byId("feed-root").querySelector(".feed-compose").open = true;
      byId("feed-body").focus();
    },
    refreshDemo() {
      const previousFriend = friendId;
      syncScope();
      if (scope === "friends" && previousFriend !== friendId) { posts = []; cursor = null; render(); load(); return; }
      if (!bridge?.demo()?.enabled) demos.clear();
      // 只替换演示卡片，保留真人评论草稿、展开状态与正在处理互动的节点。
      if (bridge?.user() && byId("feed-root")?.children.length && demoSignature !== currentDemoSignature()) {
        const target = byId("feed-posts");
        target.querySelectorAll('[data-demo="true"],.feed-empty').forEach((item) => { bridge.releaseArtwork(item); item.remove(); });
        target.append(...demoPosts().map(card));
        if (!target.children.length) target.append(el("p", "empty-state feed-empty", "还没有人发布推荐。分享第一部你喜欢的电影吧。"));
        demoSignature = currentDemoSignature();
        renderAside([...posts, ...demoPosts()]);
      }
    },
  };
})();

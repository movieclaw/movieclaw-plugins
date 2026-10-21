"use strict";

// 社区资料、通知与治理只使用当前会话；弹窗的读取有独立代次，旧账号和旧目标不能回填。
window.MovieFriendsCommunity = (() => {
  let bridge;
  let own = null;
  let profileVersion = 0;
  let noticeVersion = 0;
  const list = (value) => Array.isArray(value) ? value : [];
  const byId = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const current = (version) => bridge.user() && version === bridge.sessionVersion();
  function action(text, fn, style = "button-outline") {
    const button = window.MovieFriendsIcons.label(el("button", `button ${style}`), text); button.type = "button";
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const session = bridge.sessionVersion(); button.disabled = true;
      try { await fn(); } catch (error) { if (current(session) && !error.staleSession) bridge.toast(bridge.safeError(error), true); }
      finally { button.disabled = false; }
    });
    return button;
  }
  function dialog(id, title) {
    let target = byId(id);
    if (target) return target;
    target = el("dialog", "community-dialog"); target.id = id;
    const heading = el("div", "dialog-heading");
    const label = el("h2", "", title); label.id = `${id}-title`;
    target.setAttribute("aria-labelledby", label.id);
    heading.append(label, action("关闭 ✕", () => target.close(), "button-quiet"));
    const body = el("div", "community-dialog-content"); body.id = `${id}-content`; body.setAttribute("aria-live", "polite");
    target.append(heading, body); document.body.append(target);
    return target;
  }
  function setUnread(value) {
    const count = Math.max(0, Number(value) || 0);
    byId("notification-count").textContent = count > 99 ? "99+" : String(count);
    byId("notification-count").hidden = !count;
    byId("notification-bell").setAttribute("aria-label", count ? `通知，${count}条未读` : "通知");
  }
  async function refreshUnread() {
    const session = bridge.sessionVersion();
    const result = await bridge.api("/community/notifications/unread");
    if (current(session)) setUnread(result.unread_count);
  }
  function renderSettings() {
    const root = byId("community-settings"); root.replaceChildren();
    if (!own) return;
    const section = el("section", "panel profile-settings");
    const heading = el("div", "privacy-group-heading");
    const title=el("h3","","个人资料");title.append(window.MovieFriendsHelp.create("个人资料","头像随 MovieClaw 账号同步，请在主程序账号设置中修改。头像和简介只展示给有权查看你主页的影友；显示姓名在下方公开名片设置中修改。"));heading.append(el("span", "group-number", "ME"),title);
    heading.append(action("查看我的主页 ↗", () => openProfile({ ...own.profile, is_self: true }), "button-quiet"));
    const form = el("form"); form.id = "community-profile-form";
    const label = el("label", "", "主程序账号头像");
    const choices = el("div", "account-avatar-note");
    choices.append(bridge.avatar(own.profile), el("span", "field-help", "随主程序账号同步"));
    const bioLabel = el("label", "", "个人简介（选填）"); bioLabel.htmlFor = "profile-bio";
    const bio = el("textarea"); bio.id = "profile-bio"; bio.name="bio"; bio.rows = 2; bio.maxLength = 300; bio.value = own.profile.bio || ""; bio.placeholder = "说说你喜欢怎样的电影";
    const submit = el("button", "button button-outline", "保存个人资料"); submit.type = "submit";
    form.append(label, choices, bioLabel, bio, submit);
    const draft=window.MovieFriendsSettings.bind(form,{key:"personal-profile",session:bridge.sessionVersion()});
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); const session = bridge.sessionVersion(); submit.disabled = true;
      try {
        const result = await bridge.api("/community/profile", { method: "PATCH", body: { bio: bio.value.trim() } });
        if (!current(session)) return;
        own = result; bio.value=result.profile.bio||"";draft.saved();bridge.onProfileUpdated?.(result.profile); bridge.toast("个人资料已保存。");
      } catch (error) { if (current(session) && !error.staleSession) bridge.toast(bridge.safeError(error), true); }
      finally { submit.disabled = false; }
    });
    section.append(heading, form); root.append(section);
    const blocks = el("details", "community-blocks");
    blocks.append(el("summary", "", "我屏蔽的影友")); const rows = el("div"); blocks.append(rows);
    blocks.addEventListener("toggle", async () => {
      if (!blocks.open) return; const session = bridge.sessionVersion(); rows.replaceChildren(el("p", "busy-text", "正在读取…"));
      try {
        const result = await bridge.api("/community/blocks"); if (!current(session)) return;
        rows.replaceChildren(...list(result.blocks).map((block) => {
          const row = el("div", "community-row");
          row.append(bridge.avatar(block.user), el("span", "", block.user.nickname || "影友"), action("取消屏蔽", async () => {
            await bridge.api(`/community/blocks/${encodeURIComponent(block.user.id)}`, { method: "DELETE" });
            if (current(session)) { row.remove(); bridge.toast("已取消屏蔽。"); }
          }, "button-quiet")); return row;
        }));
        if (!result.blocks?.length) rows.append(el("p", "empty-inline", "暂未屏蔽任何影友。"));
      } catch (error) { if (current(session)) rows.replaceChildren(el("p", "empty-inline", bridge.safeError(error))); }
    });
    section.append(blocks);
    const admin = byId("community-admin"); admin.replaceChildren();
    if (own.is_admin || own.profile.role === "admin") renderAdmin(admin);
  }
  function renderAdmin(root) {
    const panel = el("details", "community-admin panel"); panel.id = "admin-report-panel";
    panel.append(el("summary", "", "举报处理 · 管理员"));
    const rows = el("div");
    const filter = el("select"); filter.id = "admin-report-filter"; filter.setAttribute("aria-label", "举报状态");
    for (const [value, text] of [["pending", "待处理"], ["resolved", "已处理"], ["all", "全部"]]) { const option = el("option", "", text); option.value = value; filter.append(option); }
    async function load(before) {
      const session = bridge.sessionVersion();
      try {
        const result = await bridge.api(`/community/admin/reports?status=${filter.value}&limit=30${before ? `&before=${encodeURIComponent(before)}` : ""}`);
        if (!current(session)) return; if (!before) rows.replaceChildren();
        for (const report of list(result.reports)) {
          const row = el("article", "report-card"); row.append(el("strong", "", `${report.target_type !== "post" ? "评论" : "推荐"} #${report.target_id}`));
          const body = report.target?.body || report.target_body;
          if (typeof body === "string") row.append(el("p", "", body));
          row.append(el("p", "muted", `举报原因：${report.reason || "未填写"}`));
          const actions = el("div", "row-actions");
          for (const [value, text] of [["hide", "隐藏内容"], ["unhide", "恢复显示"], ["dismiss", "忽略举报"]]) actions.append(action(text, async () => { await bridge.api(`/community/admin/reports/${encodeURIComponent(report.id)}/resolve`, { method: "POST", body: { action: value } }); if (current(session)) await load(); }));
          row.append(actions); rows.append(row);
        }
        if (!rows.children.length) rows.append(el("p", "empty-inline", "暂无此状态的举报。"));
        if (result.next_before) { const more = action("更多举报", async () => { more.remove(); await load(result.next_before); }, "button-quiet"); rows.append(more); }
      } catch (error) { if (current(session)) rows.replaceChildren(el("p", "empty-inline", bridge.safeError(error))); }
    }
    panel.addEventListener("toggle", () => { if (panel.open) load(); }); filter.addEventListener("change", () => load());
    panel.append(filter, rows); root.append(panel);
  }
  async function openProfile(user, context = {}) {
    const session = bridge.sessionVersion(); const version = ++profileVersion;
    const target = dialog("person-dialog", "影友主页"); const body = byId("person-dialog-content");
    bridge.releaseArtwork(body);
    target.dataset.demo = String(Boolean(user.is_demo)); body.replaceChildren(el("p", "busy-text", "正在读取主页…")); if (!target.open) window.MovieFriendsUI.open(target);
    const id = user.user_id || user.id;
    const relationship = context.relationship_id || user.relationship_id;
    const remote = Boolean(!user.is_self && relationship && (user.is_remote || context.is_remote));
    if (remote && context.status && context.status !== "accepted") {
      const header = el("div", "person-profile-heading"); header.append(bridge.avatar(user), el("h3", "", user.nickname || "跨服影友"));
      body.replaceChildren(header, el("p", "field-help", "双方确认好友关系后，可查看对方授权的资料。"));
      return;
    }
    try {
      const result = await bridge.api(`/community/profiles/${encodeURIComponent(id)}${remote ? `?relationship_id=${encodeURIComponent(relationship)}` : ""}`);
      if (!current(session) || version !== profileVersion || !target.open) return;
      let profile = result.profile;
      let data = result.public_profile;
      if (profile.is_demo) data = list(bridge.demo()?.users).find((entry) => entry.user.id === id)?.data;
      if (remote) {
        const shared = await bridge.api(`/friends/${encodeURIComponent(relationship)}`);
        if (!current(session) || version !== profileVersion || !target.open) return;
        data = shared.data;
        profile = { ...profile, avatar_data: data.avatar_data || null, avatar_id: data.avatar_id || profile.avatar_id, bio: data.bio || profile.bio };
      }
      let recommendations = {posts:[],next_before:null};
      if (!remote && !profile.is_demo) {
        recommendations = await bridge.api(`/feed?author_id=${encodeURIComponent(profile.id)}`);
        if (!current(session) || version !== profileVersion || !target.open) return;
      }
      const history = list(data?.history).slice(0, 5), favorites = list(data?.favorites);
      const posts = list(recommendations.posts);
      const hero = el("section", "profile-hero");
      const cover = favorites[0] || history[0]?.media || posts[0]?.media;
      if (cover) { const art = bridge.mediaArtwork(cover, profile.is_demo, false); art.classList.add("profile-cover"); art.setAttribute("aria-hidden", "true"); hero.append(art); }
      const header = el("div", "person-profile-heading");
      const identity = el("div", "profile-identity");
      const name = el("h3", "", profile.nickname);
      if (profile.is_demo) name.append(el("span", "demo-badge", "演示"));
      identity.append(el("p", "eyebrow", "A LIFE IN FILMS"), name);
      if (profile.bio) identity.append(el("p", "profile-biography", profile.bio));
      const scope = el("div", "profile-scope");
      scope.append(el("span", "profile-scope-label", remote ? "好友授权" : "公开名片"));
      if (profile.is_demo) scope.append(bridge.presence({ ...profile, presence: data?.presence }, data));
      identity.append(scope);
      header.append(bridge.avatar(profile), identity);
      const metrics = el("div", "profile-metrics");
      for (const [count, label] of [[history.length, "近期记录"], [favorites.length, "喜欢电影"], [posts.length ? `${posts.length}${recommendations.next_before ? "+" : ""}` : 0, "公开推荐"]]) {
        if (count) { const metric = el("div"); metric.append(el("strong", "", count), el("span", "", label)); metrics.append(metric); }
      }
      hero.append(header, metrics);
      const sections = el("div", "profile-sections");
      if (history.length) sections.append(profileShelf(remote ? "已授权的观影记录" : "公开的近期观看", bridge.historyShelf(history, profile.is_demo), history.length));
      if (favorites.length) {
        const shelf = bridge.movieShelf(favorites, profile.is_demo); shelf.classList.add("profile-favorite-shelf");
        sections.append(profileShelf("公开的喜欢电影", shelf, favorites.length));
      }
      if (posts.length) {
        const section = el("section", "profile-section profile-recommendations"), heading = el("h4", "", "公开推荐"), entries = el("div", "profile-recommendation-list");
        const count = el("span", "profile-section-count", posts.length + (recommendations.next_before ? "+" : ""));
        heading.append(count);section.append(heading, entries);sections.append(section);
        function appendPosts(items) {
          for (const post of items) {
            const card = el("article", "profile-recommendation"), copy = el("div", "profile-recommendation-copy");card.dataset.postId=post.id;
            copy.append(bridge.mediaTitle(post),el("small","field-help",[bridge.mediaMeta(post),bridge.dateText(post.created_at)].filter(Boolean).join(" · ")));
            if (Number.isInteger(post.rating) && post.rating>=1 && post.rating<=5) {const stars=el("span","profile-recommendation-rating");for(let score=1;score<=5;score++)stars.append(window.MovieFriendsIcons.create("star",score<=post.rating?"is-filled":""));stars.setAttribute("aria-label",`${post.rating} / 5 星`);copy.append(stars);}
            if(post.body)copy.append(el("p","profile-recommendation-body",post.body));
            copy.append(action("查看动态与互动",async()=>{target.close();await bridge.focusPost(post.id);},"button-quiet"));
            card.append(bridge.mediaPoster(post),copy);entries.append(card);
          }
        }
        appendPosts(posts);
        if(recommendations.next_before) {
          let before=recommendations.next_before,shown=posts.length;
          const more=action("更多公开推荐",async()=>{
            const page=await bridge.api(`/feed?${new URLSearchParams({author_id:profile.id,before})}`);
            if(!current(session)||version!==profileVersion||!target.open)return;
            appendPosts(list(page.posts));shown+=list(page.posts).length;before=page.next_before;count.textContent=shown+(before?"+":"");
            if(!before)more.remove();
          },"button-outline");section.append(more);
        }
      }
      if (!history.length && !favorites.length && !posts.length) sections.append(el("p", "profile-empty", "暂无向你公开的内容。"));
      const footer = el("div", "profile-footer");
      if (profile.is_demo) footer.append(el("p", "field-help", "演示身份与观看行为，影片为真实作品。"));
      else footer.append(window.MovieFriendsHelp.create("可见范围","这里只展示已公开或向你授权的内容。"));
      if (profile.is_self) footer.append(action("编辑个人资料", () => { target.close(); bridge.editProfile?.(); }, "button-outline"));
      else if (!profile.is_demo) footer.append(action("屏蔽这位影友", () => blockUser(user, context), "button-quiet"));
      sections.append(footer);
      body.replaceChildren(hero, sections);
    } catch (error) { if (current(session) && version === profileVersion) body.replaceChildren(el("p", "empty-inline", bridge.safeError(error))); }
  }
  function profileShelf(label, shelf, count) {
    const section = el("section", "profile-section");
    const heading = el("div", "profile-section-heading");
    const title = el("h4", "", label); title.append(el("span", "profile-section-count", count));
    const controls = el("div", "profile-shelf-controls");
    for (const [direction, glyph] of [[-1, "chevron-left"], [1, "chevron-right"]]) {
      const button = action("", () => shelf.scrollBy({ left: direction * shelf.clientWidth * .8, behavior: "smooth" }), "button-quiet");
      button.append(window.MovieFriendsIcons.create(glyph));
      button.setAttribute("aria-label", `向${direction < 0 ? "左" : "右"}浏览${label}`); controls.append(button);
    }
    shelf.tabIndex = 0; shelf.setAttribute("role", "region"); shelf.setAttribute("aria-label", label);
    heading.append(title, controls); section.append(heading, shelf);
    return section;
  }
  async function blockUser(user, context = {}) {
    const session = bridge.sessionVersion();
    if (!window.confirm(`屏蔽「${user.nickname || "这位影友"}」？双方将无法继续查看彼此的动态或互动，可在设置取消。`)) return;
    const relationship = context.relationship_id || user.relationship_id;
    const remote = Boolean(user.is_remote || context.is_remote);
    if(remote&&/^network:[0-9a-f]{64}$/.test(user.id||"")){await bridge.api("/network/blocks",{method:"POST",body:{actor:user.id}});if(current(session)){byId("person-dialog")?.close();bridge.toast("已屏蔽这位互联影友。");await bridge.onBlocked?.();}return;}
    if (remote && !relationship) throw new Error("无法确认跨服好友关系，请从对应动态重新操作。");
    await bridge.api("/community/blocks", { method: "POST", body: remote ? { relationship_id: relationship } : { user_id: user.user_id || user.id } });
    if (!current(session)) return;
    byId("person-dialog")?.close(); bridge.toast("已屏蔽这位影友。"); await bridge.onBlocked?.();
  }
  function reportPost(post, targetType = "post") {
    if (post.relationship_id) { bridge.toast("跨服推荐可以按好友关系屏蔽，不会把对方帖号提交到本服。", true); return; }
    const target = dialog("report-dialog", "举报内容"); const body = byId("report-dialog-content");
    const form = el("form"); const label = el("label", "", "举报原因"); label.htmlFor = "report-reason";
    const reason = el("textarea"); reason.id = "report-reason"; reason.rows = 3; reason.maxLength = 500; reason.required = true; reason.placeholder = "说明你认为不适合公开展示的原因";
    const submit = el("button", "button button-primary", "提交举报"); submit.type = "submit";
    form.append(label, reason, submit); body.replaceChildren(form); if (!target.open) window.MovieFriendsUI.open(target);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); const session = bridge.sessionVersion(); submit.disabled = true;
      try { await bridge.api("/community/reports", { method: "POST", body: { target_type: targetType, target_id: post.id, reason: reason.value.trim() } }); if (current(session)) { target.close(); bridge.toast("举报已提交给本服管理员。"); } }
      catch (error) { if (current(session) && !error.staleSession) bridge.toast(bridge.safeError(error), true); }
      finally { submit.disabled = false; }
    });
  }
  async function notifications(before) {
    const session = bridge.sessionVersion(); const version = ++noticeVersion;
    const target = dialog("notifications-dialog", "通知"); const body = byId("notifications-dialog-content"); if (!target.open) window.MovieFriendsUI.open(target);
    if (!before) body.replaceChildren(el("p", "busy-text", "正在读取通知…"));
    try {
      const result = await bridge.api(`/community/notifications?limit=30${before ? `&before=${encodeURIComponent(before)}` : ""}`);
      if (!current(session) || version !== noticeVersion || !target.open) return;
      setUnread(result.unread_count); if (!before) body.replaceChildren();
      for (const notice of list(result.notifications)) {
        const row = action("", async () => {
          const read = await bridge.api(`/community/notifications/${encodeURIComponent(notice.id)}/read`, { method: "POST" });
          if (!current(session)) return; setUnread(read.unread_count); target.close();
          if (notice.target_type === "relationship") await bridge.showRequests(); else if (notice.target_type === "post") await bridge.focusPost(notice.target_id);
        }, `notification-row${notice.read_at ? "" : " is-unread"}`);
        const copy = el("span", "person-copy"); copy.append(el("strong", "", notice.label), el("small", "", notice.created_at ? new Date(notice.created_at).toLocaleString("zh-CN") : ""));
        row.append(bridge.avatar(notice.actor || {}), copy); body.append(row);
      }
      if (!body.children.length) body.append(el("p", "empty-inline", "暂时没有新通知。"));
      if (result.next_before) { const more = action("更早的通知", async () => { more.remove(); await notifications(result.next_before); }, "button-quiet"); body.append(more); }
    } catch (error) { if (current(session) && version === noticeVersion) body.replaceChildren(el("p", "empty-inline", bridge.safeError(error))); }
  }
  return {
    configure(value) { bridge = value; byId("notification-bell").addEventListener("click", () => notifications()); },
    async start() { const session = bridge.sessionVersion(); const result = await bridge.api("/community/profile"); if (!current(session)) return; own = result; bridge.onProfileUpdated?.(result.profile); renderSettings(); byId("notification-bell").disabled = false; await refreshUnread(); },
    refreshUnread, openProfile, blockUser, reportPost, reportComment: (comment) => reportPost(typeof comment.id==="string"&&/^n[0-9]+$/.test(comment.id)?{...comment,id:Number(comment.id.slice(1))}:comment,typeof comment.id==="string"&&comment.id.startsWith("n")?"network_comment":"comment"),
    refreshDemo() { if (!bridge.demo()?.enabled && byId("person-dialog")?.dataset.demo === "true") byId("person-dialog").close(); },
    reset() { own = null; profileVersion += 1; noticeVersion += 1; byId("community-settings").replaceChildren(); byId("community-admin").replaceChildren(); byId("notification-bell").disabled = true; setUnread(0); for (const id of ["person-dialog", "notifications-dialog", "report-dialog"]) if (byId(id)?.open) byId(id).close(); },
  };
})();

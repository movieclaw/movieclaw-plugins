"use strict";

// 兼容只导出部分主题变量的宿主：配色模式、文字与表面必须一起更新。
// 仅读取同源父页面的公开设计变量，不读取账号、接口或用户设置。
(() => {
  const root = document.documentElement;
  const tokens = ["bg", "surface-sidebar", "surface-main", "surface-raised", "surface-inset",
    "text", "text-muted", "text-faint", "line", "line-strong", "accent", "accent-strong",
    "accent-soft", "glass-fill", "ok", "warn", "danger"];
  const media = window.matchMedia("(prefers-color-scheme: light)");
  let host;
  try { if (window.parent !== window) host = window.parent.document; } catch { /* 独立页保留本地配色。 */ }

  function isLightText(value) {
    const hex = value.trim().match(/^#([a-f\d]{6}|[a-f\d]{3})$/i);
    const rgb = value.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i);
    let parts;
    if (hex) {
      const full = hex[1].length === 3 ? [...hex[1]].map(c => c + c).join("") : hex[1];
      parts = [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16));
    } else if (rgb) parts = rgb.slice(1).map(Number);
    return parts ? parts[0] * .2126 + parts[1] * .7152 + parts[2] * .0722 > 145 : null;
  }

  function update() {
    let scheme = "dark";
    if (host?.documentElement) {
      scheme = media.matches ? "light" : "dark";
      const style = window.parent.getComputedStyle(host.documentElement);
      const declared = style.colorScheme.trim();
      // 旧版的 normal 不代表浅色，优先根据宿主真实主文字判断深浅。
      const lightText = isLightText(style.getPropertyValue("--text") ||
        window.parent.getComputedStyle(host.body || host.documentElement).color);
      if (declared === "light" || declared === "dark") scheme = declared;
      else if (lightText !== null) scheme = lightText ? "dark" : "light";
      for (const name of tokens) {
        const value = style.getPropertyValue(`--${name}`).trim();
        const key = `--mc-${name}`;
        if (value) {
          if (root.style.getPropertyValue(key) !== value) root.style.setProperty(key, value);
        } else if (root.style.getPropertyValue(key)) root.style.removeProperty(key);
      }
      const theme = host.documentElement.dataset.theme || "silver";
      if (root.dataset.mcTheme !== theme) root.dataset.mcTheme = theme;
      root.dataset.mcEmbedded = "true";
      // 未声明 color-scheme 的旧宿主没有可靠的透明画布，明确使用同一主题底色。
      root.toggleAttribute("data-mc-opaque-canvas", declared !== "light" && declared !== "dark");
    }
    if (root.dataset.mcColorScheme !== scheme) root.dataset.mcColorScheme = scheme;
    if (root.style.colorScheme !== scheme) root.style.colorScheme = scheme;
  }

  update();
  media.addEventListener("change", update);
  if (host) {
    let queued = false;
    const observer = new MutationObserver(() => {
      if (queued) return;
      queued = true;
      queueMicrotask(() => { queued = false; update(); });
    });
    const options = { attributes: true, attributeFilter: ["style", "class", "data-theme", "data-mc-theme"] };
    observer.observe(host.documentElement, options);
    observer.observe(root, options); // 宿主加载完成后再次注入变量时也重新配对深浅色。
    window.addEventListener("pagehide", () => observer.disconnect(), { once: true });
  }
})();

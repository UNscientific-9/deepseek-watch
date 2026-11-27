(() => {
  // 全站脚本：复制 RSS 地址 / QQ 群号与主题切换。没有第三方请求，也没有 Cookie。
  async function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    const ok = document.execCommand("copy");
    input.remove();
    return ok;
  }

  const copyStatus = document.getElementById("copy-status");
  // 每个按钮各自计时：同一页可能有多条 RSS 地址，共用一个 timer 会让
  // 先点的按钮永远停在「已复制」。
  const copyTimers = new WeakMap();
  for (const node of document.querySelectorAll("[data-copy]")) {
    // 带提示气泡的图标按钮：复制反馈写进气泡，不能替换按钮 textContent（会连图标一起删掉）。
    const tip = node.querySelector("[data-copy-tip]");
    const labelNode = tip || node;
    // 记住原始按钮文案；不能用「当前 textContent」，否则连续点击会把「已复制」当成原文。
    if (!node.dataset.copyLabel) node.dataset.copyLabel = labelNode.textContent;
    node.addEventListener("click", async (event) => {
      event.preventDefault();
      const text = node.getAttribute("data-copy") || node.textContent || "";
      const label = node.dataset.copyLabel;
      let ok = false;
      try {
        ok = await copy(text);
      } catch {
        ok = false;
      }
      window.clearTimeout(copyTimers.get(node) || 0);
      if (tip) {
        tip.textContent = ok ? `已复制群号 ${text}` : "复制失败，请重试";
        node.classList.toggle("is-copied", ok);
        node.classList.toggle("is-copy-failed", !ok);
      } else {
        node.textContent = ok ? "已复制" : "复制失败";
      }
      if (copyStatus) {
        const name = node.dataset.copyName || "内容";
        copyStatus.textContent = ok
          ? `已复制${name} ${text}`
          : "复制失败，请重试";
      }
      // 失败也恢复原文案：否则按钮会一直停在「复制失败」，用户不知道还能再试。
      copyTimers.set(
        node,
        window.setTimeout(() => {
          if (tip) {
            tip.textContent = label;
            node.classList.remove("is-copied", "is-copy-failed");
          } else {
            node.textContent = label;
          }
          if (copyStatus) copyStatus.textContent = "";
        }, 1600),
      );
    });
  }

  const root = document.documentElement;
  const toggle = document.querySelector("[data-theme-toggle]");
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const supportsViewTransition = typeof document.startViewTransition === "function";
  let themeTransitioning = false;

  function effectiveTheme() {
    return root.dataset.theme || (systemDark.matches ? "dark" : "light");
  }

  function syncThemeUi() {
    const isDark = effectiveTheme() === "dark";
    if (themeMeta) themeMeta.setAttribute("content", isDark ? "#0C161B" : "#F4F1E9");
    if (toggle) {
      const label = isDark ? "切换到浅色主题" : "切换到深色主题";
      toggle.setAttribute("aria-label", label);
      toggle.setAttribute("title", label);
    }
  }

  function applyTheme(next) {
    root.dataset.theme = next;
    try {
      localStorage.setItem("ds-theme", next);
    } catch {
      // 无法保存时本次切换仍然生效。
    }
    syncThemeUi();
  }

  function toggleTheme(event) {
    if (themeTransitioning) return;

    const next = effectiveTheme() === "dark" ? "light" : "dark";

    // 不支持 View Transitions 或用户偏好减少动效时，直接切换。
    if (!supportsViewTransition || reduceMotion.matches) {
      applyTheme(next);
      return;
    }

    // 固定取按钮中心：无论点在按钮哪个位置或键盘触发，圆心都落在按钮正中。
    const button = event.currentTarget instanceof Element ? event.currentTarget : toggle;
    const rect = button ? button.getBoundingClientRect() : null;
    const x = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
    const y = rect ? rect.top + rect.height / 2 : window.innerHeight / 2;

    root.style.setProperty("--theme-vt-x", `${x}px`);
    root.style.setProperty("--theme-vt-y", `${y}px`);

    // 浅色展开、深色收拢。
    root.classList.remove("theme-switch-to-light", "theme-switch-to-dark");
    root.classList.add(next === "light" ? "theme-switch-to-light" : "theme-switch-to-dark");

    themeTransitioning = true;
    let transition;
    try {
      transition = document.startViewTransition(() => applyTheme(next));
    } catch {
      themeTransitioning = false;
      root.classList.remove("theme-switch-to-light", "theme-switch-to-dark");
      applyTheme(next);
      return;
    }
    transition.finished.finally(() => {
      themeTransitioning = false;
      root.classList.remove("theme-switch-to-light", "theme-switch-to-dark");
    });
  }

  if (toggle) {
    toggle.addEventListener("click", toggleTheme);
  }

  systemDark.addEventListener?.("change", syncThemeUi);
  syncThemeUi();

  // 打印 / 存 PDF 时展开所有折叠区，避免 EVIDENCE / 判级依据被截掉。
  const openedForPrint = new Set();
  window.addEventListener("beforeprint", () => {
    openedForPrint.clear();
    for (const details of document.querySelectorAll("details")) {
      if (!details.open) {
        openedForPrint.add(details);
        details.open = true;
      }
    }
  });
  window.addEventListener("afterprint", () => {
    for (const details of openedForPrint) details.open = false;
    openedForPrint.clear();
  });
})();

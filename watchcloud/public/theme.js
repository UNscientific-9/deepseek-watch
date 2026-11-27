(() => {
  try {
    const saved = localStorage.getItem("ds-theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.dataset.theme = saved;
    }
  } catch {
    // 隐私模式或禁用 localStorage 时跟随系统主题。
  }
})();

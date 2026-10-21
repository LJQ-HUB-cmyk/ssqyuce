// 临时探针：把 api() 的调用结果暴露给 Playwright（交付前删除，见 tests/frontend/README.md）
// 仅在 URL 带 ?qa_probe=1 时挂载，不影响正常页面。
(function () {
  if (location.search.indexOf("qa_probe=1") < 0) return;
  window.__QA = {
    // 直接用页面里的 api()，走完整 401/403/423 分支
    call: async (path, opts) => {
      try {
        const r = await api(path, opts || {});
        return { ok: true, data: r };
      } catch (e) {
        return { ok: false, status: e.status, msg: e.message, payload: e.payload };
      }
    },
    state: () => ({
      authenticated: AUTH.authenticated,
      enforced: AUTH.enforced,
      csrfLen: (AUTH.csrf || "").length,
      overlayShown: (($("#authOverlay") || {}).style || {}).display === "flex",
      overlayHint: (($("#authHint") || {}).textContent || ""),
    }),
    resetCsrf: () => { AUTH.csrf = ""; },
    setCsrf: (v) => { AUTH.csrf = v; },
  };
})();

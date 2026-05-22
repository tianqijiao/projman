(function () {
  const storageKey = `projman:scroll:${window.location.pathname}${window.location.search}`;

  function restoreScrollPosition() {
    const rawY = window.sessionStorage.getItem(storageKey);
    if (!rawY) {
      return;
    }
    window.sessionStorage.removeItem(storageKey);
    const y = Number(rawY);
    if (!Number.isFinite(y)) {
      return;
    }
    window.requestAnimationFrame(function () {
      window.scrollTo(0, y);
    });
  }

  function bindScrollPreservingForms() {
    document.querySelectorAll("form[data-preserve-scroll]").forEach(function (form) {
      form.addEventListener("submit", function () {
        window.sessionStorage.setItem(storageKey, String(window.scrollY));
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    restoreScrollPosition();
    bindScrollPreservingForms();
  });
})();

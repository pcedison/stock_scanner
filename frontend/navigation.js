(function exposeNavigationHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerNavigation = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createNavigationModule() {
  // Responsive navigation / mobile menu helpers. Stateless aside from the DOM
  // they read; `query` is app.js's `$` and `breakpoint` is MOBILE_NAV_BREAKPOINT.
  function createNavigation({ query, breakpoint }) {
    const $ = query;
    const MOBILE_NAV_BREAKPOINT = breakpoint;

    function syncAccountPanelPlacement() {
      if (typeof document === "undefined") return;
      const panel = $("#account-panel");
      const desktopSlot = $("#desktop-account-panel-slot");
      const mobileSlot = $("#mobile-account-panel-slot");
      if (!panel || !desktopSlot || !mobileSlot) return;
      const useMobileSlot = window.matchMedia(`(max-width: ${MOBILE_NAV_BREAKPOINT}px)`).matches;
      const targetSlot = useMobileSlot ? mobileSlot : desktopSlot;
      if (panel.parentElement !== targetSlot) targetSlot.appendChild(panel);
    }

    function isMobileNavigation() {
      return typeof window !== "undefined" && window.matchMedia(`(max-width: ${MOBILE_NAV_BREAKPOINT}px)`).matches;
    }

    function setScanNavExpanded(expanded) {
      if (typeof document === "undefined") return;
      const toggle = $("#scan-nav-toggle");
      const subitems = $("#scan-nav-subitems");
      const icon = toggle?.querySelector(".nav-group-icon");
      if (!toggle || !subitems) return;
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      subitems.classList.toggle("open", expanded);
      if (icon) icon.textContent = expanded ? "−" : "+";
    }

    function normalizeResponsiveNavigation() {
      setScanNavExpanded(!isMobileNavigation());
      syncAccountPanelPlacement();
    }

    function setMobileMenuOpen(open) {
      const toggle = $("#mobile-menu-toggle");
      const backdrop = $("#mobile-nav-backdrop");
      document.body.classList.toggle("mobile-menu-open", open);
      if (toggle) {
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        toggle.setAttribute("aria-label", open ? "關閉功能選單" : "開啟功能選單");
      }
      if (backdrop) backdrop.classList.toggle("hidden", !open);
    }

    function closeMobileMenu() {
      setMobileMenuOpen(false);
    }

    function toggleMobileMenu() {
      setMobileMenuOpen(!document.body.classList.contains("mobile-menu-open"));
    }

    return {
      syncAccountPanelPlacement,
      isMobileNavigation,
      setScanNavExpanded,
      normalizeResponsiveNavigation,
      setMobileMenuOpen,
      closeMobileMenu,
      toggleMobileMenu,
    };
  }

  return { createNavigation };
});

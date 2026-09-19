// Theme switch for the topbar. Two states only, dark being the default, so an
// existing user sees nothing change until they click. The choice is per browser:
// it lives in localStorage and never reaches the server.
//
// The <html data-theme> attribute is set by the inline snippet in base.html,
// which runs before the stylesheet paints. Doing it here instead would show one
// dark frame to a user who chose light.
(function () {
  var KEY = "lssr-theme";
  var root = document.documentElement;

  function apply(theme) {
    if (theme === "light") {
      root.setAttribute("data-theme", "light");
    } else {
      root.removeAttribute("data-theme");
    }
    var btn = document.querySelector(".theme-toggle");
    if (btn) {
      var next = theme === "light" ? "dark" : "light";
      btn.setAttribute("aria-label", "Switch to " + next + " theme");
      btn.setAttribute("title", "Switch to " + next + " theme");
      btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
    }
  }

  function current() {
    return root.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  document.addEventListener("DOMContentLoaded", function () {
    apply(current());
    var btn = document.querySelector(".theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var next = current() === "light" ? "dark" : "light";
      apply(next);
      // A browser in private mode, or with site data blocked, throws here. The
      // theme still switches for this page; it just will not be remembered.
      try {
        window.localStorage.setItem(KEY, next);
      } catch (e) {}
    });
  });
})();

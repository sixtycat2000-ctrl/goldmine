/* app.js — shared shell: nav, language toggle, progress bar, reveals.
   All pages, defer. No dependencies. */
(function () {
  "use strict";
  var doc = document.documentElement;
  doc.classList.add("js");

  /* language toggle ------------------------------------------------ */
  var STRINGS = {}; // pages may extend
  function currentLang() {
    return doc.getAttribute("lang") === "en" ? "en" : "zh";
  }
  function applyLang(lang, persist) {
    doc.setAttribute("lang", lang === "en" ? "en" : "zh-CN");
    document.querySelectorAll("[data-zh]").forEach(function (el) {
      el.textContent = lang === "en" ? el.getAttribute("data-en") : el.getAttribute("data-zh");
    });
    document.querySelectorAll(".lang-pill button").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-lang") === lang);
    });
    if (persist) { try { localStorage.setItem("gm-lang", lang); } catch (e) {} }
    doc.dispatchEvent(new CustomEvent("gmlang", { detail: { lang: lang } }));
  }
  function initLang() {
    var stored = null;
    try { stored = localStorage.getItem("gm-lang"); } catch (e) {}
    var lang = stored || (navigator.languages && navigator.languages.some(function (l) {
      return l && l.toLowerCase().indexOf("zh") === 0;
    }) ? "zh" : (navigator.language && navigator.language.indexOf("zh") === 0 ? "zh" : "en"));
    applyLang(lang, false);
    document.querySelectorAll(".lang-pill button").forEach(function (b) {
      b.addEventListener("click", function () { applyLang(b.getAttribute("data-lang"), true); });
    });
  }

  /* nav scrolled state + progress bar ------------------------------ */
  function onScroll() {
    var y = window.scrollY || 0;
    var nav = document.querySelector(".nav");
    if (nav) nav.classList.toggle("scrolled", y > 24);
    var bar = document.querySelector(".progress");
    if (bar) {
      var h = document.documentElement.scrollHeight - window.innerHeight;
      bar.style.width = (h > 0 ? Math.min(1, y / h) * 100 : 0) + "%";
    }
  }
  window.addEventListener("scroll", onScroll, { passive: true });

  /* reveal system --------------------------------------------------- */
  function initReveals() {
    var els = [].slice.call(document.querySelectorAll(".rv"));
    function inView(el) {
      var r = el.getBoundingClientRect();
      return r.top < window.innerHeight && r.bottom > 0;
    }
    if (!("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -60px 0px" });
    els.forEach(function (el, i) {
      var sib = el.parentElement && el.parentElement.classList.contains("stagger");
      if (sib) el.style.transitionDelay = (Math.min(i, 8) * 70) + "ms";
      io.observe(el);
    });
    /* safety net: if IO callbacks stall (occluded panes, exotic embeds),
       anything sitting inside the viewport still gets revealed — content
       must never stay invisible because an animation queue froze */
    setInterval(function () {
      els.forEach(function (el) {
        if (!el.classList.contains("in") && inView(el)) el.classList.add("in");
      });
    }, 1500);
  }

  /* copy buttons ---------------------------------------------------- */
  function initCopy() {
    document.addEventListener("click", function (e) {
      var btn = e.target.closest(".copy-btn");
      if (!btn) return;
      var pre = btn.closest(".term") && btn.closest(".term").querySelector("pre");
      if (!pre) return;
      var text = pre.innerText.replace(/\u00a0/g, " ");
      function done(ok) {
        btn.classList.add("ok");
        var old = btn.getAttribute("data-label");
        btn.setAttribute("data-label", ok ? "copied" : "failed");
        setTimeout(function () { btn.classList.remove("ok"); if (old) btn.setAttribute("data-label", old); }, 1200);
      }
      function fallbackCopy() {
        try {
          var ta = document.createElement("textarea");
          ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select();
          var ok = document.execCommand("copy");
          document.body.removeChild(ta);
          done(ok);
        } catch (e) { done(false); }
      }
      if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, fallbackCopy);
      } else { fallbackCopy(); }
    });
  }

  initLang();
  initReveals();
  initCopy();
  onScroll();
  window.gmApplyLang = applyLang;
  window.gmCurrentLang = currentLang;
})();

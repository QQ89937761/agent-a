/* Message navigation uses the rendered conversation; no extra history or backend requests. */
(function () {
  "use strict";
  window.createChatNavigator = function (log, nav) {
    const list = nav.querySelector(".message-nav-list");
    let entries = [], positions = [], active = -1, frame = 0, timer = 0;
    let dirty = true, browsing = false;
    const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    function centerMarker() {
      const button = entries[active]?.button;
      if (button) list.scrollTop = button.offsetTop - list.clientHeight / 2 + button.offsetHeight / 2;
    }
    function update() {
      frame = 0;
      nav.hidden = entries.length < 2 || log.scrollHeight <= log.clientHeight + 1;
      if (nav.hidden) return;
      if (dirty) {
        const top = log.getBoundingClientRect().top;
        positions = entries.map(entry => entry.node.getBoundingClientRect().top - top + log.scrollTop);
        dirty = false;
      }
      const cursor = log.scrollTop + Math.min(96, log.clientHeight / 4);
      let index = 0;
      for (let i = 1; i < positions.length && positions[i] <= cursor; i++) index = i;
      if (log.scrollHeight - log.scrollTop - log.clientHeight < 3) index = entries.length - 1;
      if (index !== active) {
        entries[active]?.button.removeAttribute("aria-current");
        active = index;
        entries[active].button.setAttribute("aria-current", "true");
        if (!nav.matches(":hover, :focus-within")) centerMarker();
      }
    }
    function schedule() {
      if (!frame) frame = requestAnimationFrame(update);
    }
    function refresh() {
      timer = 0;
      const nodes = Array.from(log.querySelectorAll(".msg.user"));
      if (nodes.length !== entries.length || nodes.some((node, i) => node !== entries[i].node)) {
        const focused = document.activeElement;
        const focusedNode = entries.find(entry => entry.button === focused)?.node;
        entries = nodes.map(node => {
          const bubble = node.querySelector(".bubble");
          const content = bubble?.cloneNode(true);
          content?.querySelectorAll(".bubble-actions, .message-attachments").forEach(child => child.remove());
          const summary = content?.textContent.trim().replace(/\s+/g, " ") ||
            node.querySelector(".message-file, .message-image")?.textContent || node.querySelector(".message-image")?.alt ||
            window.XiaoALanguage?.translate("附件消息") || "附件消息";
          const button = document.createElement("button");
          button.type = "button";
          button.className = "message-nav-item";
          button.setAttribute("aria-controls", log.id);
          button.setAttribute("data-no-i18n", "");
          button.setAttribute("aria-label", summary.slice(0, 160));
          const label = document.createElement("span");
          label.className = "message-nav-label";
          label.textContent = summary.slice(0, 160);
          button.appendChild(label);
          button.onclick = () => {
            browsing = true;
            const top = node.getBoundingClientRect().top - log.getBoundingClientRect().top + log.scrollTop - 16;
            log.scrollTo({top: Math.max(0, top), behavior: reducedMotion() ? "instant" : "smooth"});
          };
          return {node, button};
        });
        list.replaceChildren(...entries.map(entry => entry.button));
        entries.find(entry => entry.node === focusedNode)?.button.focus({preventScroll: true});
        active = -1;
      }
      if (!nodes.length) browsing = false;
      dirty = true;
      schedule();
    }
    // Coalesce streaming mutations; scroll frames reuse measured positions.
    const observer = new MutationObserver(() => {
      if (!timer) timer = setTimeout(refresh, 100);
    });
    observer.observe(log, {childList: true, subtree: true, characterData: true});
    const resize = new ResizeObserver(() => { dirty = true; schedule(); });
    resize.observe(log);
    log.addEventListener("load", () => { dirty = true; schedule(); }, true);
    log.addEventListener("toggle", () => { dirty = true; schedule(); }, true);
    let lastScrollTop = log.scrollTop;
    log.addEventListener("scroll", () => {
      if (log.scrollTop < lastScrollTop) browsing = true;
      lastScrollTop = log.scrollTop;
      if (log.scrollHeight - log.scrollTop - log.clientHeight < 3) browsing = false;
      schedule();
    }, {passive: true});
    log.addEventListener("wheel", event => { if (event.deltaY < 0) browsing = true; }, {passive: true});
    log.addEventListener("touchstart", () => { browsing = true; }, {passive: true});
    nav.addEventListener("pointerenter", centerMarker);
    nav.addEventListener("pointerleave", () => {
      if (nav.contains(document.activeElement) && !document.activeElement.matches(":focus-visible")) document.activeElement.blur();
    });
    list.addEventListener("keydown", event => {
      const index = entries.findIndex(entry => entry.button === event.target);
      let target = index;
      if (event.key === "ArrowDown") target++;
      else if (event.key === "ArrowUp") target--;
      else if (event.key === "Home") target = 0;
      else if (event.key === "End") target = entries.length - 1;
      else if (event.key === "Escape") { event.target.blur(); return; }
      else return;
      event.preventDefault();
      entries[Math.max(0, Math.min(entries.length - 1, target))]?.button.focus({preventScroll: true});
      const button = document.activeElement;
      if (list.contains(button)) list.scrollTop = button.offsetTop - list.clientHeight / 2 + button.offsetHeight / 2;
    });
    refresh();
    return {
      isBrowsing: () => browsing,
      followLatest: () => { browsing = false; },
    };
  };
})();

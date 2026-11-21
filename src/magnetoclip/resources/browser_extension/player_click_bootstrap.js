/*
 * MagnetoClip early player-button click capture.
 *
 * Loaded in the isolated world at document_start, before page scripts can
 * register capture listeners that swallow clicks. It handles pointerup first
 * (so a page cannot cancel the subsequent click), then keyboard/programmatic
 * click events. The player overlay exposes its real handler on the button once
 * it has attached.
 */
(function installMagnetoClipEarlyClickCapture() {
  "use strict";

  if (typeof window === "undefined" || !window.addEventListener) return;
  if (window.__mcEarlyPlayerClickCaptureInstalled) return;
  window.__mcEarlyPlayerClickCaptureInstalled = true;

  const pointerActivations = new WeakMap();

  function findOverlayButton(event) {
    let path = [];
    try {
      path = event && typeof event.composedPath === "function"
        ? event.composedPath()
        : [];
    } catch (_) {}

    for (let index = 0; index < path.length; index += 1) {
      const node = path[index];
      try {
        if (node && node.classList && node.classList.contains("mc-dl-btn")) {
          return node;
        }
      } catch (_) {}
    }

    const target = event && event.target;
    try {
      if (target && target.closest) {
        const button = target.closest("button.mc-dl-btn");
        if (button) return button;
      }
    } catch (_) {}
    return null;
  }

  function stopPageHandling(event, preventDefault) {
    if (preventDefault) {
      try { event.preventDefault(); } catch (_) {}
    }
    try { event.stopPropagation(); } catch (_) {}
    try { event.stopImmediatePropagation(); } catch (_) {}
  }

  function dispatch(button, event, handlerEvent) {
    if (!button || !event) return false;
    const handler = button.__mcDispatchMagnetoClick;
    if (typeof handler !== "function") return false;

    stopPageHandling(event, event.type === "click");
    try {
      handler(handlerEvent || event);
      return true;
    } catch (error) {
      try {
        console.warn("[MagnetoClip] early player click dispatch failed", error);
      } catch (_) {}
      return false;
    }
  }

  function capturePointer(event) {
    if (!event || (typeof event.button === "number" && event.button !== 0)) return;
    const button = findOverlayButton(event);
    if (!button || typeof button.__mcDispatchMagnetoClick !== "function") return;
    // Reuse the real overlay click handler with the pointer's composed path,
    // but keep preventDefault off the original pointerup so the browser's
    // follow-up click can be swallowed as a duplicate activation.
    const handlerEvent = {
      type: "click",
      target: event.target,
      composedPath: function () {
        try { return event.composedPath(); } catch (_) { return [event.target]; }
      },
      preventDefault: function () {},
      stopPropagation: function () { try { event.stopPropagation(); } catch (_) {} },
      stopImmediatePropagation: function () { try { event.stopImmediatePropagation(); } catch (_) {} },
    };
    if (dispatch(button, event, handlerEvent)) {
      pointerActivations.set(button, Date.now());
    }
  }

  function captureClick(event) {
    const button = findOverlayButton(event);
    if (!button) return;
    const lastPointerAt = pointerActivations.get(button) || 0;
    if (lastPointerAt && Date.now() - lastPointerAt < 1200) {
      // The pointerup already ran the action; suppress the browser's follow-up
      // click so the download is not submitted twice.
      stopPageHandling(event, true);
      pointerActivations.delete(button);
      return;
    }
    dispatch(button, event);
  }

  // The early pointerup capture wins against player scripts that swallow click
  // events or call preventDefault during pointer handling. The normal click
  // path remains for keyboard activation and synthetic clicks.
  window.addEventListener("pointerup", capturePointer, true);
  window.addEventListener("click", captureClick, true);
})();

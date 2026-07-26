// Home Assistant's iOS companion app is a WKWebView that renders ingress
// add-on pages inside an <iframe>. WebKit has a long-standing bug where a
// subframe's scrolling doesn't get wired up until the frame receives focus —
// so the Numbat page won't scroll to a swipe until the user taps something.
// Safari opened directly doesn't nest the page in a subframe, so it's
// unaffected.
//
// Since the split-view layout the document itself never scrolls — each column
// is its own overflow-y container — which sidesteps the classic document-
// scroll variant of the bug. The kick remains as belt-and-braces: focus the
// frame and give the window AND every column (marked data-scrollkick) a 1px
// scroll nudge so WebKit activates their scroll views without a tap. Once on
// mount, and once more on the first interaction (the mount kick fires before
// the plan loads, when the columns may not be scrollable yet). Everything
// here is invisible and a no-op on browsers that already scroll normally.
export function installIosScrollKick(): () => void {
  const kick = () => {
    try {
      window.focus();
      const y = window.scrollY;
      window.scrollTo(0, y + 1);
      window.scrollTo(0, y);
      for (const el of Array.from(
        document.querySelectorAll<HTMLElement>("[data-scrollkick]"),
      )) {
        const top = el.scrollTop;
        el.scrollTop = top + 1;
        el.scrollTop = top;
      }
    } catch {
      // window access can throw in exotic embeddings — ignore.
    }
  };

  const onFirstInteraction = () => {
    kick();
    remove();
  };
  const remove = () => {
    window.removeEventListener("touchstart", onFirstInteraction);
    window.removeEventListener("focus", onFirstInteraction);
  };

  kick();
  window.addEventListener("touchstart", onFirstInteraction, { passive: true });
  window.addEventListener("focus", onFirstInteraction);
  return remove;
}

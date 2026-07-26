import { useSyncExternalStore } from "react";

// Chart-hover timestamp shared with the mode strip WITHOUT re-rendering the
// charts: Recharts syncs its own charts via syncId, but the strip is a
// custom component, so it subscribes to this tiny external store instead.
type Listener = () => void;

let hoverT: number | null = null;
const listeners = new Set<Listener>();

export function setHoverT(t: number | null): void {
  if (t === hoverT) return;
  hoverT = t;
  for (const l of listeners) l();
}

function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useHoverT(): number | null {
  return useSyncExternalStore(subscribe, () => hoverT);
}

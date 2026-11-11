import { useState } from "react";

// How much of the plan the charts show, in hours from now; 0 = the whole
// horizon. On a phone the full 36 h strip is unreadable and what matters is
// the next few hours, so narrow screens default to 12 h; wide screens show
// everything. The choice is remembered per browser.
export const RANGE_OPTIONS: { hours: number; label: string }[] = [
  { hours: 1, label: "1 h" },
  { hours: 6, label: "6 h" },
  { hours: 12, label: "12 h" },
  { hours: 24, label: "24 h" },
  { hours: 0, label: "All" },
];

const STORAGE_KEY = "numbat-range-hours";
const NARROW = "(max-width: 639px)";

function initialRangeHours(): number {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored !== null && RANGE_OPTIONS.some((o) => o.hours === Number(stored))) {
      return Number(stored);
    }
  } catch {
    // storage blocked: fall through to the screen-size default
  }
  return window.matchMedia(NARROW).matches ? 12 : 0;
}

export function useRangeHours(): [number, (hours: number) => void] {
  const [hours, setHours] = useState(initialRangeHours);
  const choose = (h: number) => {
    setHours(h);
    try {
      localStorage.setItem(STORAGE_KEY, String(h));
    } catch {
      // per-browser convenience only
    }
  };
  return [hours, choose];
}

/** Segmented range picker, styled like the Live | Test switch. */
export function RangeBar({ hours, onChange }: { hours: number; onChange: (h: number) => void }) {
  const seg =
    "flex flex-1 sm:flex-none cursor-pointer items-center justify-center rounded-md " +
    "border-none px-3 py-[5px] text-[12px] font-semibold transition-colors ";
  const inactive = "bg-transparent text-muted-foreground hover:text-foreground";
  const active =
    "bg-card text-foreground shadow-[0_1px_2px_rgba(0,0,0,.12)] dark:bg-[#2c2c2c] dark:shadow-none";
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-[11px] font-semibold tracking-[.06em] text-muted-foreground uppercase">
        {hours ? `Next ${hours} h` : "Whole plan"}
      </span>
      <div
        role="radiogroup"
        aria-label="Chart range"
        className="flex gap-[2px] rounded-lg border border-border bg-tab-bg p-[2px] max-sm:flex-1"
      >
        {RANGE_OPTIONS.map((o) => (
          <button
            key={o.hours}
            type="button"
            role="radio"
            aria-checked={hours === o.hours}
            onClick={() => onChange(o.hours)}
            className={seg + (hours === o.hours ? active : inactive)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

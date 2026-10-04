import { isSetbox } from "@/lib/appMode";

/** The app mark: Seedbox's sprouting crate (violet) or Setbox's crossfade (amber to pink), as in the app icons. */
export function Logo({ size = 22 }: { size?: number }) {
  return isSetbox ? <SetboxLogo size={size} /> : <SeedboxLogo size={size} />;
}

function SeedboxLogo({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <defs>
        <linearGradient id="sb-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#8a96ff" />
          <stop offset="1" stopColor="#5a3bd8" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="22" height="22" rx="5.5" fill="url(#sb-logo)" />
      <path d="M5.5 13.5h13v4.25a1.75 1.75 0 0 1-1.75 1.75h-9.5A1.75 1.75 0 0 1 5.5 17.75z" fill="#fff" fillOpacity="0.95" />
      <path d="M12 13.5V9.2" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M12 10.2c0-2.3 1.6-3.9 4-3.9 0 2.3-1.6 3.9-4 3.9z" fill="#fff" />
      <path d="M12 11.2c0-1.9-1.3-3.2-3.3-3.2 0 1.9 1.3 3.2 3.3 3.2z" fill="#fff" fillOpacity="0.8" />
    </svg>
  );
}

function SetboxLogo({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <defs>
        <linearGradient id="setbox-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ffb35c" />
          <stop offset="1" stopColor="#e0457b" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="22" height="22" rx="5.5" fill="url(#setbox-logo)" />
      <path d="M5.5 8.5C10 8.5 14 15.5 18.5 15.5" stroke="#fff" strokeWidth="1.9" strokeLinecap="round" fill="none" />
      <path d="M5.5 15.5C10 15.5 14 8.5 18.5 8.5" stroke="#fff" strokeOpacity="0.75" strokeWidth="1.9" strokeLinecap="round" fill="none" />
    </svg>
  );
}

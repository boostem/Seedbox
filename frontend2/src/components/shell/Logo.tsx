/** Seedbox mark: a seed sprouting inside a crate. */
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <defs>
        <linearGradient id="sb-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--color-accent-hover)" />
          <stop offset="1" stopColor="var(--color-brand-deep)" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="22" height="22" rx="5.5" fill="url(#sb-logo)" />
      <path d="M5.5 13.5h13v4.25a1.75 1.75 0 0 1-1.75 1.75h-9.5A1.75 1.75 0 0 1 5.5 17.75z" fill="var(--color-white)" fillOpacity="0.95" />
      <path d="M12 13.5V9.2" stroke="var(--color-white)" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M12 10.2c0-2.3 1.6-3.9 4-3.9 0 2.3-1.6 3.9-4 3.9z" fill="var(--color-white)" />
      <path d="M12 11.2c0-1.9-1.3-3.2-3.3-3.2 0 1.9 1.3 3.2 3.3 3.2z" fill="var(--color-white)" fillOpacity="0.8" />
    </svg>
  );
}

import { Disc3 } from "lucide-react";
import { useState } from "react";
import { apiUrl } from "@/api/client";
import { keyColor } from "@/lib/camelot";
import { cn } from "@/lib/cn";

export interface CoverArtProps {
  src: string | null | undefined;
  /** Rendered size in px (square). Spotify art is fetched at the smallest variant ≥ 2× this. */
  size?: number;
  alt?: string;
  /** Tints the fallback tile with the key colour. */
  camelot?: string;
  rounded?: "xs" | "sm" | "md" | "lg";
  className?: string;
  /** Fill the parent instead of a fixed size (cards). */
  fill?: boolean;
}

// Spotify CDN size codes: 64 / 300 / 640 px
const SPOTIFY = /(ab67616d0000)(b273|1e02|4851)/;

/** Picks the smallest Spotify image variant that stays sharp at `px` CSS pixels (2× DPR). */
export function spotifyImage(url: string, px: number): string {
  if (!SPOTIFY.test(url)) return url;
  const need = px * 2;
  const code = need <= 64 ? "4851" : need <= 300 ? "1e02" : "b273";
  return url.replace(SPOTIFY, `$1${code}`);
}

/** Album cover (Spotify URL or "/api/covers/<folder>/<id>" embedded art) with lazy loading and a key-tinted disc fallback. */
export function CoverArt({ src, size = 32, alt = "", camelot, rounded = "sm", className, fill }: CoverArtProps) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const r = { xs: "rounded-xs", sm: "rounded-sm", md: "rounded-md", lg: "rounded-lg" }[rounded];
  const dim = fill ? undefined : { width: size, height: size };
  const tint = keyColor(camelot);
  if (!src || failed) {
    return (
      <div
        style={{ ...dim, background: camelot ? `linear-gradient(135deg, ${tint.bg}, transparent 70%), var(--color-raised)` : undefined }}
        className={cn("grid shrink-0 place-items-center bg-raised text-fg-faint", r, fill && "aspect-square w-full", className)}
        aria-label={alt}
      >
        <Disc3 size={fill ? 40 : Math.max(12, Math.round(size * 0.45))} strokeWidth={1.5} style={camelot ? { color: tint.border } : undefined} />
      </div>
    );
  }
  return (
    <div style={dim} className={cn("relative shrink-0 overflow-hidden bg-raised", r, fill && "aspect-square w-full", className)}>
      {!loaded && (
        <div className="absolute inset-0 grid place-items-center text-fg-faint">
          <Disc3 size={fill ? 40 : Math.max(12, Math.round(size * 0.45))} strokeWidth={1.5} className="animate-pulse-soft" />
        </div>
      )}
      <img
        src={src.startsWith("/api/") ? apiUrl(src) : spotifyImage(src, fill ? 300 : size)}
        alt={alt}
        loading="lazy"
        decoding="async"
        draggable={false}
        onLoad={() => setLoaded(true)}
        onError={() => setFailed(true)}
        className={cn("relative size-full object-cover transition-opacity duration-200", loaded ? "opacity-100" : "opacity-0")}
      />
      <div className="pointer-events-none absolute inset-0 rounded-[inherit] shadow-[inset_0_0_0_1px_rgb(255_255_255/0.06)]" />
    </div>
  );
}

import { Star } from "lucide-react";
import { cn } from "@/lib/cn";
import { energyStars } from "@/lib/format";

export interface StarRatingProps {
  /** 0-5 stars… */
  stars?: number;
  /** …or an energy rating 1-10 (converted like the POPM tag: ceil(r/2)). */
  rating?: number | null;
  size?: number;
  className?: string;
}

/** Read-only 5-star display (energy rating). */
export function StarRating({ stars, rating, size = 11, className }: StarRatingProps) {
  const n = stars ?? energyStars(rating);
  return (
    <span className={cn("inline-flex items-center gap-px", className)} aria-label={`${n} of 5 stars`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <Star key={i} size={size} strokeWidth={0} className={i <= n ? "fill-warn" : "fill-fg-faint/60"} />
      ))}
    </span>
  );
}

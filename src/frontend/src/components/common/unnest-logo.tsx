import type { HTMLAttributes } from "react";
import { cn } from "@/utils/utils";

type UnnestLogoProps = HTMLAttributes<HTMLDivElement> & {
  showWordmark?: boolean;
  markClassName?: string;
  wordmarkClassName?: string;
};

export function UnnestLogo({
  showWordmark = false,
  markClassName,
  wordmarkClassName,
  className,
  ...props
}: UnnestLogoProps) {
  return (
    <div
      role="img"
      aria-label="Unnest"
      className={cn("inline-flex items-center justify-center gap-3", className)}
      {...props}
    >
      <img
        src="/brand/unnest-mark.png"
        alt=""
        aria-hidden="true"
        className={cn(
          "h-full w-auto object-contain brightness-0 dark:invert",
          markClassName,
        )}
      />
      {showWordmark && (
        <span
          aria-hidden="true"
          className={cn(
            "font-display text-2xl font-bold tracking-tight text-foreground",
            wordmarkClassName,
          )}
        >
          Unnest
        </span>
      )}
    </div>
  );
}

export default UnnestLogo;

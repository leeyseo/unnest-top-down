import type { ReactNode } from "react";
import { customPreLoadImageUrl } from "@/customization/utils/custom-pre-load-image-url";
import { cn } from "@/utils/utils";

interface UnnestEmptyStateProps {
  image: string;
  title: ReactNode;
  description: ReactNode;
  children?: ReactNode;
  className?: string;
  titleTestId?: string;
  descriptionTestId?: string;
}

export default function UnnestEmptyState({
  image,
  title,
  description,
  children,
  className,
  titleTestId,
  descriptionTestId,
}: UnnestEmptyStateProps) {
  return (
    <section
      className={cn(
        "relative flex h-full min-h-80 w-full items-center justify-center overflow-hidden px-6 py-12",
        className,
      )}
    >
      <div
        aria-hidden="true"
        className="absolute h-64 w-64 rounded-full bg-beta-background opacity-60 blur-3xl"
      />
      <div className="relative flex w-full max-w-xl flex-col items-center text-center">
        <div className="rounded-full border border-beta-foreground bg-background p-1.5 shadow-lg">
          <img
            src={customPreLoadImageUrl(image)}
            alt=""
            aria-hidden="true"
            draggable={false}
            className="h-28 w-28 select-none rounded-full object-cover [image-rendering:pixelated]"
          />
        </div>
        <h3
          className="mt-6 font-display text-2xl font-semibold tracking-tight text-foreground"
          data-testid={titleTestId}
        >
          {title}
        </h3>
        <p
          className="mt-2 max-w-lg text-sm leading-6 text-muted-foreground"
          data-testid={descriptionTestId}
        >
          {description}
        </p>
        {children && (
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
            {children}
          </div>
        )}
      </div>
    </section>
  );
}

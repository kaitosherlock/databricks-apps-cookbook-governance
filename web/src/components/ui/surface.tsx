"use client";

/** Containers, labels and loading placeholders. */
import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-lg border border-border bg-raised shadow-card", className)}
      {...props}
    />
  );
}

export function CardHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3",
        className,
      )}
    >
      <div className="min-w-0 space-y-0.5">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {description ? (
          <p className="text-[13px] leading-relaxed text-muted">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-4 py-3.5", className)} {...props} />;
}

const badge = cva(
  "inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-2xs font-medium",
  {
    variants: {
      tone: {
        neutral: "border-border-strong bg-surface text-muted",
        primary: "border-primary bg-primary-soft text-primary",
        danger: "border-danger-border bg-danger-soft text-danger-ink",
        caution: "border-caution-border bg-caution-soft text-caution-ink",
        info: "border-info-border bg-info-soft text-info-ink",
        good: "border-good-border bg-good-soft text-good-ink",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badge> {}

/**
 * A badge always contains a word.
 *
 * Tone is reinforcement, never the message: a reader who cannot distinguish the
 * colours still gets the whole meaning from the text inside.
 */
export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badge({ tone }), className)} {...props} />;
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn("relative overflow-hidden rounded-md bg-surface", className)}
    >
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-border to-transparent" />
    </div>
  );
}

/** Announces that something is loading, for readers who cannot see a shimmer. */
export function LoadingRows({ rows = 3, label }: { rows?: number; label: string }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-9 w-full" />
      ))}
    </div>
  );
}

export function PageTitle({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-ink text-balance">{title}</h1>
        {description ? (
          <p className="max-w-2xl text-sm leading-relaxed text-muted">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

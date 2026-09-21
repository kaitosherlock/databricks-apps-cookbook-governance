"use client";

/**
 * Three levels, and only three.
 *
 * The Streamlit build shipped 110 `st.info` and 109 `st.warning` calls across
 * fifteen screens, plus 328 captions. When a page is mostly yellow boxes, the
 * one that says "removing this mask exposes the raw column values" looks
 * exactly like the one explaining a pagination quirk, and an operator learns to
 * skim both.
 *
 * So the levels are defined by consequence, not by mood:
 *
 *   danger   this action can expose data or cannot be undone
 *   caution  this will not do what you probably expect
 *   note     context. Quiet, inline, never a coloured box.
 *
 * Long explanations belong in <Explain>, not here.
 *
 * Nothing communicates by colour alone: every level carries an icon and a word,
 * so the meaning survives a greyscale screenshot and a colour-blind reader.
 */
import { AlertTriangle, CircleAlert, CircleCheck, Info, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type AlertLevel = "danger" | "caution" | "info" | "good";

const STYLES: Record<
  AlertLevel,
  { box: string; icon: typeof Info; iconClass: string; word: string }
> = {
  danger: {
    box: "border-danger-border bg-danger-soft text-danger-ink",
    icon: ShieldAlert,
    iconClass: "text-danger",
    word: "Cảnh báo",
  },
  caution: {
    box: "border-caution-border bg-caution-soft text-caution-ink",
    icon: AlertTriangle,
    iconClass: "text-caution",
    word: "Lưu ý",
  },
  info: {
    box: "border-info-border bg-info-soft text-info-ink",
    icon: Info,
    iconClass: "text-info",
    word: "Thông tin",
  },
  good: {
    box: "border-good-border bg-good-soft text-good-ink",
    icon: CircleCheck,
    iconClass: "text-good",
    word: "Hoàn tất",
  },
};

export interface AlertProps {
  level: AlertLevel;
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
  /** Renders the level word for readers who cannot rely on the icon or colour. */
  showWord?: boolean;
}

export function Alert({ level, title, children, className, showWord = true }: AlertProps) {
  const style = STYLES[level];
  const Icon = style.icon;

  return (
    <div
      role={level === "danger" ? "alert" : "note"}
      className={cn("flex gap-3 rounded-md border px-3.5 py-3 text-sm", style.box, className)}
    >
      <Icon aria-hidden className={cn("mt-0.5 size-4 shrink-0", style.iconClass)} />
      <div className="min-w-0 space-y-1">
        {title ? (
          <p className="font-semibold leading-snug">
            {showWord ? <span className="sr-only">{style.word}: </span> : null}
            {title}
          </p>
        ) : null}
        {children ? <div className="leading-relaxed [&_a]:underline">{children}</div> : null}
      </div>
    </div>
  );
}

/**
 * A quiet line of context.
 *
 * This is what most of the old `st.caption` calls should have been: present,
 * readable, and not competing with anything. It is muted but still clears AA
 * (6.8:1 in light, 8.8:1 in dark) - "de-emphasised" must not mean "unreadable".
 */
export function Note({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("text-[13px] leading-relaxed text-muted", className)}>{children}</p>;
}

/**
 * The outcome of a change whose result could not be confirmed.
 *
 * Deliberately not styled as an error: the request may well have succeeded, and
 * telling someone a change failed when it might have applied is how a grant
 * gets made twice. The only safe instruction is to go and look.
 */
export function UnknownOutcome({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex gap-3 rounded-md border border-caution-border bg-caution-soft px-3.5 py-3 text-sm text-caution-ink",
        className,
      )}
    >
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-caution" />
      <div className="min-w-0 space-y-1">
        <p className="font-semibold">Chưa xác định được kết quả</p>
        <div className="leading-relaxed">{children}</div>
      </div>
    </div>
  );
}

"use client";

/**
 * Showing an identifier, and letting someone take it.
 *
 * The Streamlit build put full names in an editable text input captioned
 * "select the box and press Ctrl+C". That control accepted edits that did
 * nothing, which is a false affordance: it looks like it changes something and
 * it does not. This is a read-only value with a real copy button.
 */
import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

export function CopyButton({
  value,
  label,
  className,
}: {
  value: string;
  label: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      // Clipboard access can be refused by the browser. Failing silently would
      // leave someone believing they had copied something, so we say nothing
      // happened rather than showing a success tick.
      setCopied(false);
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      className={cn(
        "inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-muted transition-colors hover:bg-surface hover:text-ink",
        className,
      )}
      aria-label={copied ? `Đã sao chép ${label}` : `Sao chép ${label}`}
    >
      {copied ? (
        <Check aria-hidden className="size-3.5 text-good" />
      ) : (
        <Copy aria-hidden className="size-3.5" />
      )}
      {/* Announced to screen readers without moving focus. */}
      <span aria-live="polite" className="sr-only">
        {copied ? "Đã sao chép" : ""}
      </span>
    </button>
  );
}

/** A full name or other identifier, shown in full and copyable in one click. */
export function CopyField({
  value,
  label,
  className,
}: {
  value: string;
  label: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-1 rounded-md border border-border bg-surface pl-2.5 pr-1",
        className,
      )}
    >
      <code className="min-w-0 flex-1 truncate py-1.5 text-[13px] text-ink" title={value}>
        {value}
      </code>
      <CopyButton value={value} label={label} />
    </div>
  );
}

/**
 * An identifier shown *without* a copy button, on purpose.
 *
 * Used for the string an operator must retype to confirm a change. Offering a
 * copy button there would reduce a deliberate checkpoint to one click, which is
 * the whole thing the checkpoint exists to prevent.
 */
export function ConfirmTarget({ value }: { value: string }) {
  return (
    <code className="rounded-sm border border-border bg-surface px-1.5 py-0.5 text-[13px] text-ink">
      {value}
    </code>
  );
}

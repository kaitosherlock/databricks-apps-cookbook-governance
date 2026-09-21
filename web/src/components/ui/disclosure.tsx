"use client";

/**
 * Long explanations, folded away until asked for.
 *
 * Most of the prose in the old build sat permanently on screen as captions, so
 * a screen that needed one sentence of context carried six. The content is
 * worth keeping - it encodes real Unity Catalog behaviour that is easy to get
 * wrong - it just should not be competing with the task.
 *
 * Built on <details> so it works before hydration and is keyboard-operable
 * without any JavaScript of ours.
 */
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Explain({
  title,
  children,
  className,
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <details className={cn("group rounded-md border border-border bg-surface", className)}>
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-[13px] font-medium text-ink marker:content-none">
        <ChevronRight
          aria-hidden
          className="size-3.5 shrink-0 text-muted transition-transform group-open:rotate-90"
        />
        {title}
      </summary>
      <div className="space-y-2 border-t border-border px-3 py-2.5 text-[13px] leading-relaxed text-muted">
        {children}
      </div>
    </details>
  );
}

/**
 * Raw API payloads, kept off the working surface.
 *
 * Someone reconciling against Catalog Explorer needs this; nobody doing the
 * daily job does.
 */
export function TechnicalDetails({
  payload,
  label = "Chi tiết kỹ thuật",
}: {
  payload: unknown;
  label?: string;
}) {
  return (
    <Explain title={label}>
      <p>
        Phần này dành cho người cần đối chiếu với API Databricks. Thao tác hằng ngày không cần
        đọc ở đây.
      </p>
      <pre className="max-h-96 overflow-auto rounded-md border border-border bg-raised p-3 text-2xs text-ink">
        {JSON.stringify(payload, null, 2)}
      </pre>
    </Explain>
  );
}

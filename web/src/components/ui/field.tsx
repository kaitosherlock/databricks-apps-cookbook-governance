"use client";

/**
 * Form fields with labels that are always present.
 *
 * A placeholder is not a label: it disappears the moment someone types, and it
 * is invisible to a screen reader as a name. Every control here takes a real
 * label, and `hideLabel` only hides it visually.
 */
import * as LabelPrimitive from "@radix-ui/react-label";
import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

const CONTROL = [
  "w-full rounded-md border border-border-strong bg-raised px-3 text-sm text-ink",
  "placeholder:text-faint",
  "disabled:cursor-not-allowed disabled:bg-surface disabled:text-muted",
  "aria-[invalid=true]:border-danger",
].join(" ");

export function Label({
  children,
  htmlFor,
  hidden,
  className,
}: {
  children: ReactNode;
  htmlFor: string;
  hidden?: boolean;
  className?: string;
}) {
  return (
    <LabelPrimitive.Root
      htmlFor={htmlFor}
      className={cn(
        "block text-[13px] font-medium text-ink",
        hidden && "sr-only",
        className,
      )}
    >
      {children}
    </LabelPrimitive.Root>
  );
}

export interface FieldProps {
  label: ReactNode;
  hideLabel?: boolean;
  hint?: ReactNode;
  error?: ReactNode;
  children: (ids: { id: string; describedBy: string | undefined }) => ReactNode;
  className?: string;
}

/**
 * Wires a label, a hint and an error to one control.
 *
 * The error sits next to the field rather than in a summary at the top of the
 * page, so the person fixing it does not have to hold the mapping in their head.
 */
export function Field({ label, hideLabel, hint, error, children, className }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(" ") || undefined;

  return (
    <div className={cn("space-y-1.5", className)}>
      <Label htmlFor={id} hidden={hideLabel}>
        {label}
      </Label>
      {children({ id, describedBy })}
      {error ? (
        <p id={errorId} role="alert" className="text-[13px] text-danger-ink">
          {error}
        </p>
      ) : null}
      {hint ? (
        <p id={hintId} className="text-[13px] leading-relaxed text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return <input ref={ref} className={cn(CONTROL, "h-9", className)} {...props} />;
  },
);

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...props }, ref) {
  return <textarea ref={ref} className={cn(CONTROL, "min-h-20 py-2", className)} {...props} />;
});

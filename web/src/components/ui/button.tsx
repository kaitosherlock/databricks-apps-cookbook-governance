"use client";

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

const button = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md",
    "text-sm font-medium transition-colors",
    "disabled:pointer-events-none disabled:opacity-50",
    "[&_svg]:size-4 [&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-ink hover:bg-primary-hover",
        outline: "border border-border-strong bg-raised text-ink hover:bg-surface",
        ghost: "text-ink hover:bg-surface",
        // Used only where the action exposes data or cannot be undone.
        danger: "bg-danger text-white hover:brightness-110",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        // 36px and 40px targets. Small is for dense table rows only, never for
        // anything destructive.
        sm: "h-8 px-2.5 text-[13px]",
        md: "h-9 px-3.5",
        lg: "h-10 px-4",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "outline", size: "md" },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof button> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild, loading, children, disabled, ...props },
  ref,
) {
  // Radix Slot forwards props onto exactly one child, so the spinner cannot be
  // added here - a second child makes React.Children.only throw at runtime.
  // asChild is only used for links, which never have a loading state.
  if (asChild) {
    return (
      <Slot ref={ref} className={cn(button({ variant, size }), className)} {...props}>
        {children}
      </Slot>
    );
  }

  return (
    <button
      ref={ref}
      className={cn(button({ variant, size }), className)}
      disabled={disabled || loading}
      // Screen readers are told the control is working, not just that it stopped
      // responding to clicks.
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Loader2 aria-hidden className="animate-spin" /> : null}
      {children}
    </button>
  );
});

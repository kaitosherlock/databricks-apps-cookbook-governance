"use client";

import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
}

export interface SelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  id?: string;
  describedBy?: string;
  disabled?: boolean;
  className?: string;
  /** Rendered when there is nothing to choose, instead of an empty menu. */
  emptyLabel?: ReactNode;
}

export function Select({
  value,
  onValueChange,
  options,
  placeholder = "Chọn…",
  id,
  describedBy,
  disabled,
  className,
  emptyLabel = "Không có lựa chọn nào",
}: SelectProps) {
  const isEmpty = options.length === 0;

  return (
    // Radix refuses an empty-string item value, and treats an empty Root value
    // as "nothing chosen yet" only when it is undefined. Mapping "" to undefined
    // is what makes the placeholder show before a choice is made.
    <SelectPrimitive.Root
      value={value === "" ? undefined : value}
      onValueChange={onValueChange}
      disabled={disabled || isEmpty}
    >
      <SelectPrimitive.Trigger
        id={id}
        aria-describedby={describedBy}
        className={cn(
          "flex h-9 w-full items-center justify-between gap-2 rounded-md border border-border-strong bg-raised px-3 text-sm text-ink",
          "data-[placeholder]:text-faint disabled:cursor-not-allowed disabled:bg-surface disabled:text-muted",
          className,
        )}
      >
        <span className="truncate">
          <SelectPrimitive.Value placeholder={isEmpty ? emptyLabel : placeholder} />
        </span>
        <SelectPrimitive.Icon>
          <ChevronDown aria-hidden className="size-4 shrink-0 text-muted" />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>

      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className={cn(
            "z-50 max-h-72 min-w-[var(--radix-select-trigger-width)] overflow-hidden",
            "rounded-md border border-border bg-raised shadow-pop animate-fade-in",
          )}
        >
          <SelectPrimitive.Viewport className="p-1">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className={cn(
                  "relative flex cursor-default select-none items-start gap-2 rounded-sm py-1.5 pl-7 pr-2 text-sm text-ink outline-none",
                  "data-[highlighted]:bg-surface data-[disabled]:opacity-50",
                )}
              >
                <SelectPrimitive.ItemIndicator className="absolute left-2 top-2">
                  <Check aria-hidden className="size-3.5 text-primary" />
                </SelectPrimitive.ItemIndicator>
                <span className="min-w-0">
                  <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                  {option.description ? (
                    <span className="mt-0.5 block text-2xs text-muted">{option.description}</span>
                  ) : null}
                </span>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}

"use client";

/**
 * Tabs that do not render what is not showing.
 *
 * Streamlit executed every tab body on every run, so opening the permissions
 * screen issued both the effective-grants read and the direct-grants read even
 * though only one was visible. Radix unmounts inactive panels by default, so
 * the hidden tab costs nothing until it is selected.
 */
import * as TabsPrimitive from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <TabsPrimitive.List
      className={cn("flex flex-wrap items-center gap-1 border-b border-border", className)}
    >
      {children}
    </TabsPrimitive.List>
  );
}

export function TabsTrigger({
  value,
  children,
  className,
}: {
  value: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.Trigger
      value={value}
      className={cn(
        "-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-muted transition-colors",
        "hover:text-ink data-[state=active]:border-primary data-[state=active]:text-ink",
        className,
      )}
    >
      {children}
    </TabsPrimitive.Trigger>
  );
}

export function TabsContent({
  value,
  children,
  className,
}: {
  value: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.Content value={value} className={cn("pt-4", className)}>
      {children}
    </TabsPrimitive.Content>
  );
}

"use client";

/**
 * A readable table with an honest CSV export.
 *
 * The export keeps two protections the Streamlit build had and that are easy to
 * lose in a rewrite: a leading apostrophe on anything Excel would read as a
 * formula, and a UTF-8 BOM so Vietnamese does not arrive as mojibake.
 */
import { Download } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "./button";
import { cn } from "@/lib/utils";

export interface Column<T> {
  key: string;
  header: string;
  /** How the cell looks. Defaults to the raw value as text. */
  cell?: (row: T) => ReactNode;
  /** How the cell exports. Defaults to the raw value. */
  value?: (row: T) => string;
  className?: string;
  headerClassName?: string;
}

export interface DataTableProps<T> {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string;
  caption: string;
  downloadName?: string;
  empty?: ReactNode;
  className?: string;
}

/** Excel treats these as the start of a formula. Neutralise, never drop. */
const RISKY = ["=", "+", "-", "@", "\t", "\r"];

function csvCell(raw: string): string {
  const inert = RISKY.some((prefix) => raw.startsWith(prefix)) ? `'${raw}` : raw;
  return `"${inert.replace(/"/g, '""')}"`;
}

function toCsv<T>(rows: T[], columns: Column<T>[]): string {
  const head = columns.map((column) => csvCell(column.header)).join(",");
  const body = rows.map((row) =>
    columns
      .map((column) => {
        const value = column.value
          ? column.value(row)
          : String((row as Record<string, unknown>)[column.key] ?? "");
        return csvCell(value);
      })
      .join(","),
  );
  return [head, ...body].join("\r\n");
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  caption,
  downloadName,
  empty,
  className,
}: DataTableProps<T>) {
  if (rows.length === 0 && empty) return <>{empty}</>;

  function download() {
    // The BOM is what makes Excel open this as UTF-8 rather than as the local
    // code page, which is the difference between "Quyền" and "Quyá»n".
    const blob = new Blob(["﻿", toCsv(rows, columns)], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${downloadName}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className={cn("space-y-2", className)}>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full border-collapse text-left text-[13px]">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={cn(
                    "whitespace-nowrap px-3 py-2 font-semibold text-ink",
                    column.headerClassName,
                  )}
                >
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr
                key={rowKey(row, index)}
                className="border-b border-border last:border-0 hover:bg-surface"
              >
                {columns.map((column) => (
                  <td key={column.key} className={cn("px-3 py-2 align-top", column.className)}>
                    {column.cell
                      ? column.cell(row)
                      : String((row as Record<string, unknown>)[column.key] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {downloadName && rows.length > 0 ? (
        <Button variant="ghost" size="sm" onClick={download}>
          <Download aria-hidden />
          Tải CSV ({rows.length} dòng)
        </Button>
      ) : null}
    </div>
  );
}

"use client";

/**
 * Choosing an object once, and keeping that choice in the URL.
 *
 * Loading stays lazy for the same reason as before: Unity Catalog has no
 * cross-metastore search, so walking every schema would hammer the workspace
 * and still be incomplete. Catalogs load on entry, schemas only for the chosen
 * catalog, objects only for the chosen schema.
 */
import { Field } from "@/components/ui/field";
import { Select, type SelectOption } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/surface";
import { ErrorState } from "./states";
import {
  KIND_LABELS,
  LEVEL_KINDS,
  OBJECT_KINDS,
  type Selection,
} from "@/lib/selection";
import { useCatalogs, useObjects, useSchemas } from "@/lib/queries";
import type { LevelKind, ObjectKind } from "@/lib/types";

export interface AssetPickerProps {
  selection: Selection;
  onChange: (changes: Partial<Selection>) => void;
  /** Allow addressing a catalog or a schema itself, not only a leaf object. */
  allowLevels?: boolean;
  className?: string;
}

export function AssetPicker({
  selection,
  onChange,
  allowLevels = false,
  className,
}: AssetPickerProps) {
  const catalogs = useCatalogs();
  const schemas = useSchemas(selection.catalog);

  const objectKind: ObjectKind = OBJECT_KINDS.includes(selection.kind as ObjectKind)
    ? (selection.kind as ObjectKind)
    : "table";
  const objects = useObjects(
    selection.catalog,
    selection.schema,
    objectKind,
  );

  const needsSchema = selection.kind !== "catalog";
  const needsObject = !["catalog", "schema"].includes(selection.kind);

  if (catalogs.isError) {
    return <ErrorState error={catalogs.error} context="Không đọc được danh sách catalog." />;
  }

  return (
    <div className={className}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {allowLevels ? (
          <Field label="Quản lý quyền ở cấp">
            {({ id }) => (
              <Select
                id={id}
                value={selection.kind}
                onValueChange={(value) =>
                  onChange({ kind: value as LevelKind, name: "" })
                }
                options={LEVEL_KINDS.map<SelectOption>((kind) => ({
                  value: kind,
                  label: KIND_LABELS[kind],
                }))}
              />
            )}
          </Field>
        ) : null}

        <Field
          label="Catalog"
          hint={
            catalogs.data && catalogs.data.items.length === 0
              ? "Danh tính thực thi chưa nhìn thấy catalog nào."
              : undefined
          }
        >
          {({ id }) =>
            catalogs.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : (
              <Select
                id={id}
                value={selection.catalog}
                onValueChange={(value) =>
                  onChange({ catalog: value, schema: "", name: "" })
                }
                options={(catalogs.data?.items ?? []).map<SelectOption>((item) => ({
                  value: item.name,
                  label: item.name,
                }))}
                emptyLabel="Không có catalog nào"
              />
            )
          }
        </Field>

        {needsSchema ? (
          <Field label="Schema">
            {({ id }) =>
              schemas.isPending && selection.catalog ? (
                <Skeleton className="h-9 w-full" />
              ) : (
                <Select
                  id={id}
                  value={selection.schema}
                  onValueChange={(value) => onChange({ schema: value, name: "" })}
                  disabled={!selection.catalog}
                  options={(schemas.data?.items ?? []).map<SelectOption>((item) => ({
                    value: item.name,
                    label: item.name,
                  }))}
                  emptyLabel="Không có schema nào"
                />
              )
            }
          </Field>
        ) : null}

        {needsObject ? (
          <>
            {!allowLevels ? (
              <Field label="Loại đối tượng">
                {({ id }) => (
                  <Select
                    id={id}
                    value={objectKind}
                    onValueChange={(value) =>
                      onChange({ kind: value as LevelKind, name: "" })
                    }
                    options={OBJECT_KINDS.map<SelectOption>((kind) => ({
                      value: kind,
                      label: KIND_LABELS[kind],
                    }))}
                  />
                )}
              </Field>
            ) : null}

            <Field label={KIND_LABELS[objectKind]}>
              {({ id }) =>
                objects.isPending && selection.schema ? (
                  <Skeleton className="h-9 w-full" />
                ) : (
                  <Select
                    id={id}
                    value={selection.name}
                    onValueChange={(value) => onChange({ name: value })}
                    disabled={!selection.schema}
                    options={(objects.data?.items ?? []).map<SelectOption>((item) => ({
                      value: item.name,
                      label: item.name,
                    }))}
                    emptyLabel={`Không có ${KIND_LABELS[objectKind].toLowerCase()} nào`}
                  />
                )
              }
            </Field>
          </>
        ) : null}
      </div>

      {schemas.isError ? (
        <div className="mt-3">
          <ErrorState error={schemas.error} context="Không đọc được danh sách schema." />
        </div>
      ) : null}
      {objects.isError ? (
        <div className="mt-3">
          <ErrorState error={objects.error} context="Không đọc được danh sách đối tượng." />
        </div>
      ) : null}
    </div>
  );
}

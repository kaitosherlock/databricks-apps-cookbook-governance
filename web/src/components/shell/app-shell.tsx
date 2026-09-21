"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  KeyRound,
  Menu,
  PencilLine,
  RefreshCw,
  Search,
  Settings,
  Table2,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import { Alert, Note } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { LoadingRows } from "@/components/ui/surface";
import { ErrorState } from "@/components/governance/states";
import { EnvironmentMarker, IdentityPanel, ScopePanel } from "./context-panel";
import { useRefreshAll, useSession } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { ApiError } from "@/lib/api";

interface NavItem {
  href: string;
  label: string;
  icon: typeof Search;
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Bắt đầu",
    items: [{ href: "/tim-tai-san/", label: "Tìm tài sản dữ liệu", icon: Search }],
  },
  {
    group: "Tài sản dữ liệu",
    items: [{ href: "/tai-san/", label: "Thông tin tài sản", icon: Table2 }],
  },
  {
    group: "Quyền truy cập",
    items: [
      { href: "/quyen/", label: "Xem quyền", icon: KeyRound },
      { href: "/thay-doi-quyen/", label: "Cấp / thu hồi quyền", icon: PencilLine },
    ],
  },
  {
    group: "Theo dõi",
    items: [{ href: "/thao-tac/", label: "Thao tác gần đây", icon: Activity }],
  },
  {
    group: "Hệ thống",
    items: [{ href: "/he-thong/", label: "Khả năng và cấu hình", icon: Settings }],
  },
];

export function AppShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const refresh = useRefreshAll();
  const [navOpen, setNavOpen] = useState(false);

  // A missing identity is a setup problem, not an error inside a screen, so it
  // replaces the whole shell rather than appearing inside it.
  if (session.isError && session.error instanceof ApiError && session.error.code === "NO_IDENTITY") {
    return <NoIdentityScreen error={session.error} />;
  }

  return (
    <div className="min-h-dvh bg-bg">
      <header className="sticky top-0 z-30 flex items-center gap-2 border-b border-border bg-raised px-3 py-2 lg:hidden">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setNavOpen((open) => !open)}
          aria-expanded={navOpen}
          aria-controls="app-nav"
          aria-label={navOpen ? "Đóng điều hướng" : "Mở điều hướng"}
        >
          {navOpen ? <X aria-hidden /> : <Menu aria-hidden />}
        </Button>
        <span className="text-sm font-semibold text-ink">Quản trị Unity Catalog</span>
        {session.data?.environment.high_risk ? (
          <span className="ml-auto rounded-sm border border-danger bg-danger-soft px-1.5 py-0.5 text-2xs font-bold uppercase text-danger-ink">
            {session.data.environment.name}
          </span>
        ) : null}
      </header>

      <div className="lg:grid lg:grid-cols-[17rem_minmax(0,1fr)]">
        <aside
          id="app-nav"
          className={cn(
            "border-b border-border bg-surface lg:sticky lg:top-0 lg:h-dvh lg:overflow-y-auto lg:border-b-0 lg:border-r",
            navOpen ? "block" : "hidden lg:block",
          )}
        >
          <div className="space-y-5 p-4">
            <div className="hidden lg:block">
              <p className="text-sm font-semibold leading-tight text-ink">
                Quản trị Unity Catalog
              </p>
              <Note className="text-2xs">Data Steward &amp; quản trị dữ liệu</Note>
            </div>

            {session.isPending ? (
              <LoadingRows rows={4} label="Đang đọc bối cảnh phiên làm việc" />
            ) : session.data ? (
              <>
                <EnvironmentMarker session={session.data} />
                <nav aria-label="Điều hướng chính">
                  <ul className="space-y-4">
                    {NAV.map((group) => (
                      <li key={group.group}>
                        <p className="mb-1 text-2xs font-semibold uppercase tracking-wide text-muted">
                          {group.group}
                        </p>
                        <ul className="space-y-0.5">
                          {group.items.map((item) => (
                            <li key={item.href}>
                              <NavLink item={item} onNavigate={() => setNavOpen(false)} />
                            </li>
                          ))}
                        </ul>
                      </li>
                    ))}
                  </ul>
                </nav>

                <div className="space-y-4 border-t border-border pt-4">
                  <IdentityPanel session={session.data} />
                  <ScopePanel session={session.data} />
                </div>

                <div className="space-y-2 border-t border-border pt-4">
                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full"
                    loading={refresh.isPending}
                    onClick={() => refresh.mutate()}
                  >
                    <RefreshCw aria-hidden />
                    Làm mới dữ liệu
                  </Button>
                  <Note className="text-2xs">
                    Đọc lại từ Databricks và bỏ mọi bản xem trước đang chờ.
                  </Note>

                  {session.data.pending_plans.length > 0 ? (
                    <Alert level="info" title="Đang có bản xem trước chờ xác nhận">
                      <p className="text-2xs">
                        Trên <code>{session.data.pending_plans[0]?.target}</code>
                      </p>
                    </Alert>
                  ) : null}

                  {session.data.unresolved_operations > 0 ? (
                    <Alert level="caution" title="Thao tác chưa xác định kết quả">
                      <p className="text-2xs">
                        {session.data.unresolved_operations} thao tác. Mở{" "}
                        <Link href="/thao-tac/" className="underline">
                          Thao tác gần đây
                        </Link>{" "}
                        và đối chiếu trước khi thử lại.
                      </p>
                    </Alert>
                  ) : null}
                </div>
              </>
            ) : session.isError ? (
              <ErrorState error={session.error} context="Không đọc được bối cảnh." />
            ) : null}
          </div>
        </aside>

        <main className="min-w-0 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-6xl">{children}</div>
        </main>
      </div>
    </div>
  );
}

function NavLink({ item, onNavigate }: { item: NavItem; onNavigate: () => void }) {
  const pathname = usePathname();
  const active = pathname.startsWith(item.href);
  const Icon = item.icon;

  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] transition-colors",
        active
          ? "bg-primary-soft font-medium text-primary"
          : "text-ink hover:bg-raised",
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" />
      {item.label}
    </Link>
  );
}

function NoIdentityScreen({ error }: { error: ApiError }) {
  return (
    <div className="mx-auto max-w-2xl px-4 py-16">
      <h1 className="text-xl font-semibold text-ink">Chưa xác định được người dùng</h1>
      <div className="mt-4 space-y-4">
        <Alert level="danger" title={error.body.message}>
          <p>{error.body.next_step}</p>
        </Alert>
        <div className="space-y-2 text-sm text-ink">
          <p className="font-medium">Nguyên nhân thường gặp</p>
          <ol className="list-decimal space-y-1 pl-5 text-muted">
            <li>Bạn đang mở ứng dụng bằng một địa chỉ khác URL chính thức của Databricks Apps.</li>
            <li>Ứng dụng đang chạy ngoài môi trường Databricks Apps.</li>
          </ol>
          <p className="pt-2 font-medium">Việc cần làm</p>
          <ul className="list-disc space-y-1 pl-5 text-muted">
            <li>Mở ứng dụng bằng đúng URL Databricks Apps của workspace.</li>
            <li>
              Nếu bạn đang phát triển trên máy cá nhân, đặt <code>GOVERNANCE_LOCAL=true</code> để
              chạy ở chế độ chỉ đọc bằng hồ sơ Databricks CLI.
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}

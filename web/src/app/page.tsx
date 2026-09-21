"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { LoadingRows } from "@/components/ui/surface";

/**
 * The front door is the search screen.
 *
 * A client-side replace rather than a server redirect: this app is exported
 * statically, so there is no server to issue a 3xx.
 */
export default function Home() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/tim-tai-san/");
  }, [router]);

  return <LoadingRows rows={2} label="Đang mở trang tìm tài sản dữ liệu" />;
}

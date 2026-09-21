const isDev = process.env.NODE_ENV === "development";

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Databricks Apps runs one command and installs Python packages only, so
  // there is no Node server at runtime. The whole front end is compiled to
  // static files that governance-app/server.py serves.
  //
  // Only applied to a real build: `output: "export"` and `rewrites` are
  // mutually exclusive, and the dev server needs the rewrite to reach the
  // Python API running separately on port 8000.
  ...(isDev ? {} : { output: "export" }),

  // No image optimiser without a Node server.
  images: { unoptimized: true },

  // Emit `about/index.html` rather than `about.html`, which keeps client-side
  // routing and the server's static fallback in agreement.
  trailingSlash: true,

  // A build that type-checks in CI but ships type errors is not a build.
  typescript: { ignoreBuildErrors: false },
  eslint: { ignoreDuringBuilds: false },

  ...(isDev
    ? {
        async rewrites() {
          // Development only. In production the API and the UI share an origin,
          // because one FastAPI process serves both.
          return [
            { source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" },
          ];
        },
      }
    : {}),
};

export default nextConfig;

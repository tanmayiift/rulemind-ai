/** @type {import('next').NextConfig} */

// `output: "export"` is OPT-IN, not the default, and that is load bearing.
//
// The dashboard is a pure client SPA — 37 pages, zero API routes, zero dynamic segments, no
// middleware, no next/image, no getServerSideProps — so it exports to plain files and can be
// served by any static host. But `next start` REFUSES to run an exported build, and
// apps/web/Dockerfile ends in `pnpm --filter @rulemind/web start`. Making the export
// unconditional would therefore break `make up`, docker-compose, and the "web app serves" check
// in scripts/smoke_compose.sh — for a setting only the static host needs.
//
// So: NEXT_OUTPUT=export produces `out/` for the static deployment; everything else keeps the
// server build it has always had. One flag, set in exactly one place (render.yaml).
const isStaticExport = process.env.NEXT_OUTPUT === "export";

const nextConfig = {
  transpilePackages: ["@rulemind/widget", "@rulemind/shared", "@rulemind/rule-engine"],
  experimental: {
    typedRoutes: true
  },
  ...(isStaticExport
    ? {
        output: "export",
        // Static hosts serve /connectors/index.html for /connectors; without this Next emits
        // /connectors.html and the route 404s on any host that does not silently try both.
        trailingSlash: true,
        // No image optimiser exists on a static host. The app uses no next/image today, so this
        // is insurance against a future import turning the build red for a non-obvious reason.
        images: { unoptimized: true }
      }
    : {})
};

export default nextConfig;

/** @type {import('next').NextConfig} */

// The dashboard is a pure client SPA — 37 pages, zero API routes, zero dynamic segments, no
// middleware, no next/image, no getServerSideProps. It therefore *could* be exported to plain
// files, and for a while it was: `NEXT_OUTPUT=export` produced `out/` for a static host.
//
// That branch is gone because the static host is gone. `render.yaml` was the only thing that
// ever set NEXT_OUTPUT, and nothing local does — compose and the Dockerfile both end in
// `pnpm --filter @rulemind/web start`, which REFUSES to run an exported build. Carrying a
// config branch no caller can reach is how a build option rots until the day someone flips it
// and finds out it no longer works.
//
// If a static deployment is ever wanted again, the branch was: output "export", plus
// trailingSlash (static hosts serve /connectors/index.html, not /connectors.html) and
// images.unoptimized (no optimiser exists on a static host).
const nextConfig = {
  transpilePackages: ["@rulemind/widget", "@rulemind/shared", "@rulemind/rule-engine"],
  experimental: {
    typedRoutes: true
  }
};

export default nextConfig;

/** @type {import('next').NextConfig} */
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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
  // Traces the exact files the server needs into .next/standalone, so the runtime image can be
  // built from that alone instead of from the whole workspace. apps/web/Dockerfile was a single
  // stage doing `COPY . .` plus a full `pnpm install`, and shipped all of it: **2.54 GB**, of
  // which the running server uses a small fraction. Standalone is what makes the multi-stage
  // split possible -- without it the runner stage has no way to know which of ~1,200 workspace
  // packages are actually reachable at request time.
  //
  // It changes the start command: standalone emits its own server.js, so the image runs
  // `node server.js` rather than `next start`.
  output: "standalone",
  // pnpm workspaces symlink into a store above apps/web, so the trace has to be rooted at the
  // repo, not at this package. Without it the standalone bundle silently omits every workspace
  // dependency and the container dies on MODULE_NOT_FOUND for @rulemind/shared.
  outputFileTracingRoot: path.join(__dirname, "../../"),
  transpilePackages: ["@rulemind/widget", "@rulemind/shared", "@rulemind/rule-engine"],
  experimental: {
    typedRoutes: true
  }
};

export default nextConfig;

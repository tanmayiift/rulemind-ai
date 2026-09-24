# Multi-stage. The previous single-stage image was 2.54 GB: it did `COPY . .` (the entire repo)
# plus a full workspace `pnpm install` INCLUDING devDependencies, then kept all of it in the
# final layer alongside the build output. The running server needs almost none of that.
#
# Three stages now, and only the third ships:
#   deps    — install the workspace (needs dev deps; next/typescript are devDependencies)
#   builder — build, with next.config.js `output: "standalone"` tracing the reachable files
#   runner  — node:20-alpine + the traced bundle. No pnpm store, no source, no dev deps.

# ---------------------------------------------------------------------------- deps
FROM node:20-alpine AS deps
WORKDIR /workspace
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable

# Manifests first, so an edit to source does not invalidate the install layer.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/shared/package.json packages/shared/package.json
COPY packages/widget/package.json packages/widget/package.json
COPY packages/rule-engine/package.json packages/rule-engine/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/sdk/package.json packages/sdk/package.json
COPY packages/sdk-js/package.json packages/sdk-js/package.json
RUN pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------- builder
FROM node:20-alpine AS builder
WORKDIR /workspace
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable

# ---------------------------------------------------------------------------
# BUILD-TIME API URL — READ THIS BEFORE DEPLOYING.
#
# Next.js substitutes NEXT_PUBLIC_* constants into the client bundle at BUILD
# time, and these ENVs are set before the build below. So whatever value is baked
# here is the value the BROWSER uses forever; the runtime `env:` block in
# helm/rulemind/templates/deployment-web.yaml (and the `environment:` block in
# docker-compose.yml) cannot change it.
#
# For a cluster deploy, build with an EMPTY value:
#     docker build --build-arg NEXT_PUBLIC_API_BASE_URL= -f apps/web/Dockerfile .
# apps/web/src/lib/api.ts does `fetch(`${apiBaseUrl}${path}`)` with paths like
# /api/v1/rules, so an empty base yields same-origin relative URLs, which the
# ingress routes to the API Service (see helm/rulemind/templates/ingress.yaml).
#
# The default below stays http://localhost:8080 because that is exactly right for
# `docker compose up`, where the API is published on the host at that port.
#
# THE REAL FIX is to stop reading this at build time: apps/web/src/lib/store.ts:51
# should resolve the API base at RUNTIME (same-origin by default, overridable),
# not from process.env.NEXT_PUBLIC_API_BASE_URL. That file is outside this change.
# ---------------------------------------------------------------------------
# EMPTY by default, so the bundle falls through to store.ts's same-origin resolution and one
# published image works in every cluster. It used to default to http://localhost:8080, which
# NEXT_PUBLIC_* substitution baked into the bundle at build time -- so anyone who pulled this
# image got a dashboard hard-wired to their own laptop, with no runtime override possible.
# Pass --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com only when the API is on a
# DIFFERENT origin from the dashboard (and then configure CORS_ORIGINS to match).
ARG NEXT_PUBLIC_API_BASE_URL=
ARG NEXT_PUBLIC_RULEMIND_DEV_API_KEY=
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_RULEMIND_DEV_API_KEY=$NEXT_PUBLIC_RULEMIND_DEV_API_KEY

COPY --from=deps /workspace/node_modules ./node_modules
COPY --from=deps /workspace/apps/web/node_modules ./apps/web/node_modules
# NOTE: this `COPY . .` is why .dockerignore matters — it copies the whole repo into the BUILDER.
# Unlike before, the builder is discarded; only the traced output below reaches the final image.
COPY . .
RUN pnpm --filter @rulemind/web build

# ---------------------------------------------------------------------------- runner
FROM node:20-alpine AS runner
WORKDIR /workspace
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# uid 10001 matches apps/python-executor/Dockerfile, packages/rulemind-decide-service/Dockerfile
# and securityContext.runAsUser in helm/rulemind/values.yaml. A cluster enforcing the
# `restricted` Pod Security Standard rejects a pod that runs as uid 0.
RUN addgroup -g 10001 -S rulemind \
  && adduser -S -D -u 10001 -G rulemind -h /home/rulemind rulemind

# The standalone bundle carries its own minimal node_modules, traced from actual imports.
# Ownership is set on COPY rather than by a later chown -R, which would duplicate every file
# into a second layer and undo much of the saving.
COPY --from=builder --chown=10001:10001 /workspace/apps/web/.next/standalone ./
# static/ is deliberately NOT traced into standalone -- Next expects it served from disk next to
# the server, so it is copied explicitly. Miss it and the app boots fine and then serves a page
# with no CSS and no JS, which looks like an app bug rather than a packaging one.
COPY --from=builder --chown=10001:10001 /workspace/apps/web/.next/static ./apps/web/.next/static
# There is deliberately no `COPY public/` line: apps/web has no public/ directory, and COPY on a
# missing path is a hard build failure, not a no-op. Add one here if static assets are ever
# introduced.

# `next start` refuses to run a standalone build; standalone emits its own server.
USER 10001
EXPOSE 3000
CMD ["node", "apps/web/server.js"]

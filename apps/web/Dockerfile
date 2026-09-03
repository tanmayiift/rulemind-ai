FROM node:20-alpine

WORKDIR /workspace
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

# ---------------------------------------------------------------------------
# BUILD-TIME API URL — READ THIS BEFORE DEPLOYING.
#
# Next.js substitutes NEXT_PUBLIC_* constants into the client bundle at BUILD
# time, and these ENVs are set before `pnpm --filter @rulemind/web build` below.
# So whatever value is baked here is the value the BROWSER uses forever; the
# runtime `env:` block in helm/rulemind/templates/deployment-web.yaml (and the
# `environment:` block in docker-compose.yml) cannot change it.
#
# For a cluster deploy, build with an EMPTY value:
#     docker build --build-arg NEXT_PUBLIC_API_BASE_URL= -f apps/web/Dockerfile .
# apps/web/src/lib/api.ts does `fetch(`${apiBaseUrl}${path}`)` with paths like
# /api/v1/rules, so an empty base yields same-origin relative URLs, which the
# ingress routes to the API Service (see helm/rulemind/templates/ingress.yaml,
# which now has /api and /ws rules — it previously had none).
#
# The default below stays http://localhost:8080 because that is exactly right for
# `docker compose up`, where the API is published on the host at that port.
#
# THE REAL FIX is to stop reading this at build time: apps/web/src/lib/store.ts:51
# should resolve the API base at RUNTIME (same-origin by default, overridable),
# not from process.env.NEXT_PUBLIC_API_BASE_URL. That file is outside this change.
# ---------------------------------------------------------------------------
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8080
ARG NEXT_PUBLIC_RULEMIND_DEV_API_KEY=
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_RULEMIND_DEV_API_KEY=$NEXT_PUBLIC_RULEMIND_DEV_API_KEY

RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/shared/package.json packages/shared/package.json
COPY packages/widget/package.json packages/widget/package.json
COPY packages/rule-engine/package.json packages/rule-engine/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/sdk/package.json packages/sdk/package.json
COPY packages/sdk-js/package.json packages/sdk-js/package.json

RUN pnpm install --frozen-lockfile

# NOTE: this `COPY . .` is why .dockerignore matters so much here — it copies the
# whole repo into the image. `.env` and `.venv-rulemind` are excluded there.
COPY . .

RUN pnpm --filter @rulemind/web build

# Non-root. There was no USER directive at all, so this ran as uid 0 and a cluster
# enforcing the `restricted` Pod Security Standard rejected the pod. uid 10001
# matches apps/python-executor/Dockerfile, packages/rulemind-decide-service/
# Dockerfile and securityContext.runAsUser in helm/rulemind/values.yaml.
# `next start` writes its incremental cache under .next/cache, so that path (and a
# writable HOME for pnpm/node) must be owned by the runtime user.
RUN addgroup -g 10001 -S rulemind \
  && adduser -S -D -u 10001 -G rulemind -h /home/rulemind rulemind \
  && mkdir -p /home/rulemind /workspace/apps/web/.next/cache \
  && chown -R rulemind:rulemind /home/rulemind /workspace/apps/web/.next /pnpm
ENV HOME=/home/rulemind
USER 10001

EXPOSE 3000

CMD ["pnpm", "--filter", "@rulemind/web", "start"]

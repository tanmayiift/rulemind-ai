FROM node:20-alpine AS base
WORKDIR /app
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable

FROM base AS deps
COPY package.json pnpm-workspace.yaml tsconfig.base.json vitest.config.ts ./
COPY apps/api/package.json apps/api/package.json
COPY apps/web/package.json apps/web/package.json
COPY apps/worker/package.json apps/worker/package.json
COPY packages/shared/package.json packages/shared/package.json
COPY packages/rule-engine/package.json packages/rule-engine/package.json
COPY packages/sdk/package.json packages/sdk/package.json
COPY packages/sdk-js/package.json packages/sdk-js/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/widget/package.json packages/widget/package.json
RUN pnpm install --frozen-lockfile

FROM deps AS build
COPY . .
RUN pnpm build

FROM base AS runtime
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/apps/api/dist ./apps/api/dist
COPY --from=build /app/apps/api/src ./apps/api/src
COPY --from=build /app/apps/api/openapi.json ./apps/api/openapi.json
COPY --from=build /app/apps/web/.next ./apps/web/.next
COPY --from=build /app/packages ./packages
COPY .env.example ./apps/web/next.config.js ./apps/web/package.json ./apps/api/package.json ./package.json ./
EXPOSE 3000 8080
CMD ["node", "apps/web/node_modules/.bin/next", "start", "-p", "3000"]

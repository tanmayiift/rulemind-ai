FROM node:20-alpine

WORKDIR /workspace
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8080
ARG NEXT_PUBLIC_RULEMIND_DEV_API_KEY=rm_live_devlocaltenantkey000000000000
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

RUN pnpm install --no-frozen-lockfile

COPY . .

RUN pnpm --filter @rulemind/web build

EXPOSE 3000

CMD ["pnpm", "--filter", "@rulemind/web", "start"]

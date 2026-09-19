# syntax=docker/dockerfile:1
FROM node:22-alpine AS base
RUN corepack enable && corepack prepare pnpm@10 --activate
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM deps AS checks
COPY . .
CMD ["sh", "-c", "pnpm test && pnpm lint"]

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* are baked into the client bundle at build time.
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_APP_NAME
ARG NEXT_PUBLIC_BETTER_AUTH_URL
ENV NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL \
    NEXT_PUBLIC_APP_NAME=$NEXT_PUBLIC_APP_NAME \
    NEXT_PUBLIC_BETTER_AUTH_URL=$NEXT_PUBLIC_BETTER_AUTH_URL \
    NEXT_TELEMETRY_DISABLED=1
# Server-only placeholders keep the standalone build deterministic; runtime
# secrets and operator containment values are supplied by env_file.
ENV API_URL=http://build.invalid \
    BETTER_AUTH_SECRET=build_time_placeholder_secret_min_32_chars_xx \
    BETTER_AUTH_URL=http://build.invalid \
    DATABASE_URL=postgresql://build:build@build.invalid:5432/build \
    REDIS_URL=redis://build.invalid:6379 \
    PASSKEY_RP_ID=build.invalid \
    BFX_OPERATOR_USER_ID=build-operator \
    BFX_OPERATOR_ROLE=admin
RUN pnpm build

FROM base AS runner
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/scripts ./scripts
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]

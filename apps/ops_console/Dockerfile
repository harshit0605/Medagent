# Production image for the Next.js ops console.
#
# In Coolify: deploy as an application with the base directory set to
# apps/ops_console and build pack "Dockerfile". Runtime env (ORCHESTRATOR_URL,
# GATEWAY_URL, the *_API_KEY secrets, WhatsApp/Google config) is injected by
# Coolify at run time — the console reads it server-side at request time, so it
# is NOT needed at build time (no NEXT_PUBLIC_ vars are used).
#
# There is no lockfile in this app, so `npm install` (not `npm ci`).

# ---- build ----
FROM node:22 AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . ./
RUN npm run build

# ---- runtime ----
FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/next.config.ts ./next.config.ts
EXPOSE 3000
# `next start` honours $PORT (default 3000).
CMD ["npm", "run", "start"]

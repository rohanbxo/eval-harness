# Production image for the public read-only demo's dashboard.
#
# The build context is the REPO ROOT so that one Railway service setting
# (RAILWAY_DOCKERFILE_PATH=deploy/web.Dockerfile) works for both services and
# neither needs a "Root Directory". Only web/ is actually copied.
#
# Differs from web/Dockerfile, which is the local compose image and runs
# `next dev` against a bind mount.
#
# NEXT_PUBLIC_* values are inlined into the client bundle at BUILD time, so they
# arrive as build arguments -- Railway passes every service variable as one --
# and must be declared in the stage that uses them.

FROM node:22-slim AS deps
WORKDIR /app
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund

FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./

# Set on the Railway service as:
#   NEXT_PUBLIC_API_BASE_URL = https://${{ api.RAILWAY_PUBLIC_DOMAIN }}
#   NEXT_PUBLIC_READ_ONLY    = true
ARG NEXT_PUBLIC_API_BASE_URL=""
ARG NEXT_PUBLIC_READ_ONLY="true"
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL} \
    NEXT_PUBLIC_READ_ONLY=${NEXT_PUBLIC_READ_ONLY}

# Nothing is prerendered (`export const dynamic = "force-dynamic"`), so the build
# succeeds with the API unreachable -- which it is, at build time.
RUN npm run build

FROM node:22-slim AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# `output: "standalone"` in next.config.ts produces server.js plus only the
# node_modules it actually needs.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# No web/public in this repo, so there is nothing else to copy. Add a line here
# if static assets are ever introduced -- standalone does not include them.

EXPOSE 3000
# server.js binds to $HOSTNAME and listens on $PORT. Docker sets HOSTNAME to the
# container ID at runtime, so Next bound to that one interface and Railway's proxy
# got "Application failed to respond". It is forced here rather than with ENV,
# which the runtime value overrides. PORT is left alone: Railway sets it, and
# PORT=3000 above is only the local default.
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 exec node server.js"]

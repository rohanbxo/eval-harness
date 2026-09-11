# Dev-friendly image: the compose file bind-mounts ./web over /app and keeps
# /app/node_modules and /app/.next as anonymous volumes, so dependencies are
# installed here at image build time and survive the mount.
FROM node:22-slim

WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]

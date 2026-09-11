import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The dashboard is a live view over the API. Nothing is prerendered at build
  // time, so `next build` succeeds with the API completely unreachable.
  experimental: {},
  eslint: {
    // Linting is a separate, explicit step (`npm run lint`) so a lint failure
    // never masks a build failure.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;

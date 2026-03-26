/** @type {import('next').NextConfig} */
const nextConfig = {
  transpilePackages: ["@rulemind/widget", "@rulemind/shared", "@rulemind/rule-engine"],
  experimental: {
    typedRoutes: true
  }
};

export default nextConfig;

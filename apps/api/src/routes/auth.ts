import type { FastifyInstance } from "fastify";

export async function registerAuthRoutes(app: FastifyInstance) {
  app.get("/auth/me", async (request) => {
    return {
      user: request.auth
    };
  });
}

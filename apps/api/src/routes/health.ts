import type { FastifyInstance } from "fastify";

export async function registerHealthRoutes(app: FastifyInstance) {
  app.get("/health", async () => ({
    status: "ok",
    service: "rulemind-api",
    timestamp: new Date().toISOString()
  }));

  app.get("/health/ready", async (_request, reply) => {
    const { store, cache } = app.services;
    const [database, redis] = await Promise.all([store.ping(), cache.ping()]);

    if (!database || !redis) {
      reply.code(503);
    }

    return {
      status: database && redis ? "ready" : "degraded",
      checks: {
        database,
        redis
      }
    };
  });

  app.get("/metrics", async (_request, reply) => {
    reply.type("text/plain; version=0.0.4");
    return app.services.metrics.render();
  });
}

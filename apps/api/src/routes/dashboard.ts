import type { FastifyInstance } from "fastify";
import { requirePermission } from "../auth/middleware";

export async function registerDashboardRoutes(app: FastifyInstance) {
  app.get("/api/v1/dashboard/summary", async (request, reply) => {
    requirePermission(request, "dashboard:read", reply);
    const environment = typeof request.query === "object" ? (request.query as { environment?: never }).environment : undefined;
    return app.services.store.dashboardSummary(environment);
  });
}

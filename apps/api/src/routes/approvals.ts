import type { FastifyInstance } from "fastify";
import { approvalDecisionSchema } from "@rulemind/shared";
import { requirePermission } from "../auth/middleware";

export async function registerApprovalRoutes(app: FastifyInstance) {
  app.get("/api/v1/approvals", async (request, reply) => {
    requirePermission(request, "approval:read", reply);
    return app.services.store.listApprovals(request.auth.environment);
  });

  app.post("/api/v1/approvals/:id/approve", async (request, reply) => {
    requirePermission(request, "approval:decide", reply);
    const decision = approvalDecisionSchema.parse(request.body);
    const approval = await app.services.store.getApproval(String((request.params as { id: string }).id));

    if (!approval) {
      reply.code(404);
      return { message: "Approval not found." };
    }

    if (approval.makerId === decision.checkerId) {
      reply.code(400);
      return { message: "Maker and checker must be different." };
    }

    return app.services.store.updateApproval(approval.id, {
      status: "approved",
      checkerId: decision.checkerId,
      comment: decision.comment
    });
  });

  app.post("/api/v1/approvals/:id/reject", async (request, reply) => {
    requirePermission(request, "approval:decide", reply);
    const decision = approvalDecisionSchema.parse(request.body);
    const approval = await app.services.store.getApproval(String((request.params as { id: string }).id));

    if (!approval) {
      reply.code(404);
      return { message: "Approval not found." };
    }

    if (approval.makerId === decision.checkerId) {
      reply.code(400);
      return { message: "Maker and checker must be different." };
    }

    return app.services.store.updateApproval(approval.id, {
      status: "rejected",
      checkerId: decision.checkerId,
      comment: decision.comment
    });
  });
}

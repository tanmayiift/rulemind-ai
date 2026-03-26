import { pathToFileURL } from "node:url";
import Fastify from "fastify";
import cors from "@fastify/cors";
import rateLimit from "@fastify/rate-limit";
import sensible from "@fastify/sensible";
import swagger from "@fastify/swagger";
import swaggerUi from "@fastify/swagger-ui";
import { ZodError } from "zod";
import { authenticateRequest } from "./auth/middleware";
import type { AppServices } from "./app-services";
import { RedisCache } from "./cache/redis";
import { loadConfig } from "./config";
import { createStore } from "./db";
import { createLogger } from "./observability/logger";
import { RuleMindMetrics } from "./observability/metrics";
import { initTracing } from "./observability/tracing";
import { registerApiKeyRoutes } from "./routes/api-keys";
import { registerApprovalRoutes } from "./routes/approvals";
import { registerAssetRoutes } from "./routes/assets";
import { registerAuthRoutes } from "./routes/auth";
import { registerDashboardRoutes } from "./routes/dashboard";
import { registerHealthRoutes } from "./routes/health";
import { registerRuleRoutes } from "./routes/rules";

declare module "fastify" {
  interface FastifyInstance {
    services: AppServices;
  }

  interface FastifyRequest {
    auth: Awaited<ReturnType<typeof authenticateRequest>>;
    traceId: string;
  }
}

export async function buildApp() {
  const config = loadConfig();
  await initTracing(config);
  const logger = createLogger(config);
  const store = createStore(config);
  await store.migrate();
  const cache = new RedisCache(config.redisUrl, config.cacheTtlSeconds);
  const metrics = new RuleMindMetrics();

  const app = Fastify({
    loggerInstance: logger,
    bodyLimit: config.maxRequestBodyBytes
  });

  app.services = {
    config,
    store,
    cache,
    metrics,
    logger
  };

  const configuredOrigins = config.corsOrigins
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const allowedOrigins = new Set(
    configuredOrigins.flatMap((origin) => {
      try {
        const url = new URL(origin);

        if (url.hostname === "localhost") {
          return [origin, origin.replace("localhost", "127.0.0.1")];
        }

        if (url.hostname === "127.0.0.1") {
          return [origin, origin.replace("127.0.0.1", "localhost")];
        }
      } catch {
        return [origin];
      }

      return [origin];
    })
  );

  await app.register(cors, {
    origin(origin, callback) {
      if (!origin || allowedOrigins.has(origin)) {
        callback(null, true);
        return;
      }

      callback(new Error("Origin not allowed by CORS."), false);
    },
    methods: ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization", "x-api-key", "x-trace-id"]
  });
  await app.register(sensible);
  await app.register(rateLimit, {
    max: config.rateLimitRpm,
    timeWindow: "1 minute"
  });
  await app.register(swagger, {
    openapi: {
      info: {
        title: "RuleMind API",
        version: "0.1.0"
      }
    }
  });
  await app.register(swaggerUi, {
    routePrefix: "/docs",
    uiConfig: {
      docExpansion: "list"
    }
  });

  app.addHook("onRequest", async (request) => {
    request.traceId = request.headers["x-trace-id"]
      ? String(request.headers["x-trace-id"])
      : `tr_${String(request.id).replace(/[^a-zA-Z0-9_-]/g, "")}`;
  });

  app.addHook("preHandler", async (request) => {
    if (
      request.url.startsWith("/health") ||
      request.url.startsWith("/metrics") ||
      request.url.startsWith("/docs")
    ) {
      return;
    }

    try {
      request.auth = await authenticateRequest(request, config, store);
    } catch (error) {
      logger.warn({
        event: "auth.failed",
        reason: error instanceof Error ? error.message : String(error),
        ip: request.ip,
        traceId: request.traceId
      });
      throw error;
    }
  });

  app.addHook("onSend", async (request, reply, payload) => {
    reply.header("x-trace-id", request.traceId);
    return payload;
  });

  await registerHealthRoutes(app);
  await registerAuthRoutes(app);
  await registerRuleRoutes(app);
  await registerAssetRoutes(app);
  await registerApprovalRoutes(app);
  await registerDashboardRoutes(app);
  await registerApiKeyRoutes(app);

  app.setErrorHandler((error, request, reply) => {
    if (error instanceof ZodError) {
      reply.code(400).send({
        message: "Validation failed.",
        issues: error.issues.map((issue) => ({
          path: issue.path.join("."),
          message: issue.message
        })),
        traceId: request.traceId
      });
      return;
    }

    if (!reply.sent) {
      const statusCode =
        typeof (error as { statusCode?: unknown }).statusCode === "number"
          ? ((error as { statusCode: number }).statusCode >= 400 ? (error as { statusCode: number }).statusCode : 500)
          : reply.statusCode >= 400
            ? reply.statusCode
            : 500;

      reply.code(statusCode).send({
        message: error instanceof Error ? error.message : String(error),
        traceId: request.traceId
      });
    }
  });

  return app;
}

async function start() {
  const app = await buildApp();
  await app.listen({
    host: app.services.config.host,
    port: app.services.config.port
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  start().catch((error) => {
    process.stderr.write(`${String(error)}\n`);
    process.exitCode = 1;
  });
}

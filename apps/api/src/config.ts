import { z } from "zod";

function parseRequestSize(value: unknown) {
  if (typeof value === "number") {
    return value;
  }

  if (typeof value !== "string" || value.trim() === "") {
    return undefined;
  }

  const normalized = value.trim().toLowerCase();
  const match = normalized.match(/^(\d+)(b|kb|mb)?$/);

  if (!match) {
    return undefined;
  }

  const amount = Number(match[1]);
  const unit = match[2] ?? "b";
  const multiplier = unit === "mb" ? 1024 * 1024 : unit === "kb" ? 1024 : 1;

  return amount * multiplier;
}

const configSchema = z.object({
  nodeEnv: z.enum(["development", "test", "production"]).default("development"),
  host: z.string().default("0.0.0.0"),
  port: z.coerce.number().int().positive().default(8080),
  databaseAdapter: z.enum(["file", "postgres", "sqlite", "mongodb"]).default("file"),
  databaseUrl: z.string().default("postgresql://rulemind:rulemind@localhost:5432/rulemind"),
  sqlitePath: z.string().default("./.runtime/rulemind.sqlite"),
  mongodbUrl: z.string().default("mongodb://localhost:27017/rulemind"),
  fileStorePath: z.string().default("./.runtime/data"),
  redisUrl: z.string().optional(),
  cacheTtlSeconds: z.coerce.number().int().positive().default(3600),
  authMode: z.enum(["none", "apikey", "jwt"]).default("none"),
  jwtIssuer: z.string().optional(),
  issuerBaseUrl: z.string().optional(),
  jwtAudience: z.string().default("rulemind"),
  jwtJwksUri: z.string().optional(),
  clientId: z.string().optional(),
  sessionSecret: z.string().default("change-me"),
  jwtSecret: z.string().default("change-me"),
  apiKeySalt: z.string().default("change-me"),
  pythonExecutorUrl: z.string().default("http://localhost:8001"),
  pythonExecutorSecret: z.string().default("change-me"),
  maxPythonExecutionMs: z.coerce.number().int().positive().default(2000),
  maxBatchSize: z.coerce.number().int().positive().default(10000),
  maxRequestBodyBytes: z.coerce.number().int().positive().default(1048576),
  evaluationTimeoutMs: z.coerce.number().int().positive().default(5000),
  corsOrigins: z.string().default("http://localhost:3000"),
  rateLimitRpm: z.coerce.number().int().positive().default(600),
  logLevel: z.enum(["fatal", "error", "warn", "info", "debug", "trace", "silent"]).default("info"),
  otelExporterOtlpEndpoint: z.string().optional(),
  metricsEnabled: z
    .union([z.boolean(), z.enum(["true", "false"])])
    .default(true)
    .transform((value) => (typeof value === "boolean" ? value : value === "true"))
});

export type AppConfig = z.infer<typeof configSchema>;

export function loadConfig(): AppConfig {
  return configSchema.parse({
    nodeEnv: process.env.NODE_ENV,
    host: process.env.HOST,
    port: process.env.PORT,
    databaseAdapter: process.env.DATABASE_ADAPTER,
    databaseUrl: process.env.DATABASE_URL,
    sqlitePath: process.env.SQLITE_PATH,
    mongodbUrl: process.env.MONGODB_URL,
    fileStorePath: process.env.FILE_STORE_PATH,
    redisUrl: process.env.REDIS_URL,
    cacheTtlSeconds: process.env.CACHE_TTL,
    authMode: process.env.AUTH_MODE,
    jwtIssuer: process.env.JWT_ISSUER ?? process.env.ISSUER_BASE_URL,
    issuerBaseUrl: process.env.ISSUER_BASE_URL,
    jwtAudience: process.env.JWT_AUDIENCE,
    jwtJwksUri: process.env.JWT_JWKS_URI,
    clientId: process.env.CLIENT_ID,
    sessionSecret: process.env.SESSION_SECRET,
    jwtSecret: process.env.JWT_SECRET,
    apiKeySalt: process.env.API_KEY_SALT,
    pythonExecutorUrl: process.env.PYTHON_EXECUTOR_URL,
    pythonExecutorSecret: process.env.PYTHON_EXECUTOR_SECRET,
    maxPythonExecutionMs: process.env.MAX_PYTHON_EXECUTION_MS,
    maxBatchSize: process.env.MAX_BATCH_SIZE,
    maxRequestBodyBytes: parseRequestSize(process.env.MAX_REQUEST_SIZE) ?? process.env.MAX_REQUEST_BODY_BYTES,
    evaluationTimeoutMs: process.env.EVALUATION_TIMEOUT_MS,
    corsOrigins: process.env.CORS_ORIGINS,
    rateLimitRpm: process.env.RATE_LIMIT_RPM,
    logLevel: process.env.LOG_LEVEL,
    otelExporterOtlpEndpoint: process.env.OTEL_EXPORTER_OTLP_ENDPOINT,
    metricsEnabled: process.env.METRICS_ENABLED
  });
}

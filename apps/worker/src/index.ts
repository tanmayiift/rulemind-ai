import { Worker } from "bullmq";
import IORedis from "ioredis";
import pino from "pino";

const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });
const redisUrl = process.env.REDIS_URL ?? "redis://localhost:6379";
const connection = new IORedis(redisUrl, { maxRetriesPerRequest: null });

function createQueueWorker(queueName: string) {
  return new Worker(
    queueName,
    async (job) => {
      logger.info({
        msg: "processing job",
        queueName,
        jobId: job.id,
        payload: job.data
      });

      return {
        queueName,
        processedAt: new Date().toISOString()
      };
    },
    { connection }
  );
}

const workers = ["batch-evaluate", "export-run", "outcome-import"].map(createQueueWorker);

workers.forEach((worker) => {
  worker.on("completed", (job) => {
    logger.info({ msg: "job completed", queueName: worker.name, jobId: job.id });
  });
  worker.on("failed", (job, error) => {
    logger.error({ msg: "job failed", queueName: worker.name, jobId: job?.id, error: error.message });
  });
});

process.on("SIGINT", async () => {
  await Promise.all(workers.map((worker) => worker.close()));
  await connection.quit();
  process.exit(0);
});

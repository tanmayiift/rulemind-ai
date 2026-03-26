import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { buildApp } from "./server";

async function main() {
  const app = await buildApp();
  await app.ready();
  const document = app.swagger();
  const outputPath = join(process.cwd(), "apps/api/openapi.json");
  await writeFile(outputPath, JSON.stringify(document, null, 2), "utf8");
  await app.close();
  process.stdout.write(`OpenAPI written to ${outputPath}\n`);
}

main().catch((error) => {
  process.stderr.write(`${String(error)}\n`);
  process.exitCode = 1;
});

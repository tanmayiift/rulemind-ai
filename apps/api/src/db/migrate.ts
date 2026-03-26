import { createStore } from "./index";
import { loadConfig } from "../config";

async function main() {
  const config = loadConfig();
  const store = createStore(config);
  await store.migrate();
  process.stdout.write(`Migrated ${config.databaseAdapter} adapter\n`);
}

main().catch((error) => {
  process.stderr.write(`${String(error)}\n`);
  process.exitCode = 1;
});

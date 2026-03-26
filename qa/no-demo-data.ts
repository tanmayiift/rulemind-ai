import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

const patterns = [/acme/i, /demo/i, /example\.com/i, /john/i, /jane/i, /test@/i, /sample/i, /dummy/i, /lorem/i, /ipsum/i];

async function walk(path: string): Promise<string[]> {
  const entries = await readdir(path, { withFileTypes: true });
  const files: string[] = [];

  for (const entry of entries) {
    const fullPath = join(path, entry.name);

    if (entry.isDirectory()) {
      if (["dist", ".next", "node_modules"].includes(entry.name)) {
        continue;
      }

      files.push(...(await walk(fullPath)));
      continue;
    }

    if (/\.(ts|tsx|js|jsx|json|md)$/.test(entry.name)) {
      files.push(fullPath);
    }
  }

  return files;
}

async function main() {
  const root = "/Users/tanmaykumar/Downloads/RuleMind.AI";
  const resultsDir = join(root, "qa", "results");
  await mkdir(resultsDir, { recursive: true });

  const files = [...(await walk(join(root, "apps"))), ...(await walk(join(root, "packages")))];
  const findings: Array<{ file: string; matches: string[] }> = [];

  for (const file of files) {
    const content = await readFile(file, "utf8");
    const matches = patterns.filter((pattern) => pattern.test(content)).map((pattern) => pattern.source);

    if (matches.length > 0) {
      findings.push({
        file,
        matches
      });
    }
  }

  await writeFile(
    join(resultsDir, "no-demo-data.json"),
    JSON.stringify(
      {
        generatedAt: new Date().toISOString(),
        passed: findings.length === 0,
        findings
      },
      null,
      2
    ),
    "utf8"
  );

  process.stdout.write(`No-demo-data report written to ${join(resultsDir, "no-demo-data.json")}\n`);
}

main().catch((error) => {
  process.stderr.write(`${String(error)}\n`);
  process.exitCode = 1;
});

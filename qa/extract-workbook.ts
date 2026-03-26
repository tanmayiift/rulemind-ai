import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { inflateRawSync } from "node:zlib";

interface SheetManifest {
  name: string;
  rows: Array<Record<string, string>>;
}

function columnIndex(label: string) {
  return [...label].reduce((value, char) => value * 26 + (char.charCodeAt(0) - 64), 0) - 1;
}

async function readArchiveEntries(path: string) {
  const { readFile } = await import("node:fs/promises");
  const buffer = await readFile(path);
  const text = buffer.toString("binary");
  const entries = new Map<string, Buffer>();
  let offset = 0;

  while (offset < buffer.length) {
    const signature = buffer.readUInt32LE(offset);

    if (signature !== 0x04034b50) {
      break;
    }

    const compressionMethod = buffer.readUInt16LE(offset + 8);
    const compressedSize = buffer.readUInt32LE(offset + 18);
    const fileNameLength = buffer.readUInt16LE(offset + 26);
    const extraLength = buffer.readUInt16LE(offset + 28);
    const fileName = text.slice(offset + 30, offset + 30 + fileNameLength);
    const dataStart = offset + 30 + fileNameLength + extraLength;
    const dataEnd = dataStart + compressedSize;
    const entry = buffer.subarray(dataStart, dataEnd);
    entries.set(fileName, compressionMethod === 8 ? inflateRawSync(entry) : entry);
    offset = dataEnd;
  }

  return entries;
}

function extractSharedStrings(xml: string) {
  return [...xml.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map((match) =>
    match[1]
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
  );
}

function extractRows(xml: string, sharedStrings: string[]) {
  const rows = [...xml.matchAll(/<row[\s\S]*?<\/row>/g)].map((match) => match[0]);
  return rows.map((row) => {
    const cells = [...row.matchAll(/<c[^>]*r="([A-Z]+)\d+"[^>]*?(?:t="([^"]+)")?[^>]*>([\s\S]*?)<\/c>/g)];
    const values = new Map<number, string>();

    cells.forEach((cell) => {
      const [, column, type, body] = cell;
      const shared = body.match(/<v>(\d+)<\/v>/);
      const inline = body.match(/<t[^>]*>([\s\S]*?)<\/t>/);
      const value =
        type === "s" && shared
          ? sharedStrings[Number(shared[1])] ?? ""
          : inline?.[1]?.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">") ?? body.match(/<v>([\s\S]*?)<\/v>/)?.[1] ?? "";
      values.set(columnIndex(column), value);
    });

    const headers = Array.from({ length: Math.max(...values.keys(), 0) + 1 }, (_, index) => values.get(index) ?? "");
    return headers;
  });
}

async function main() {
  const root = "/Users/tanmaykumar/Downloads/RuleMind.AI";
  const workbookPath = join(root, "Test Cases", "rulemind_test_suite.xlsx");
  const resultsDir = join(root, "qa", "results");
  await mkdir(resultsDir, { recursive: true });

  const entries = await readArchiveEntries(workbookPath);
  const workbookXml = entries.get("xl/workbook.xml")?.toString("utf8") ?? "";
  const relsXml = entries.get("xl/_rels/workbook.xml.rels")?.toString("utf8") ?? "";
  const sharedStrings = extractSharedStrings(entries.get("xl/sharedStrings.xml")?.toString("utf8") ?? "");

  const relationshipMap = new Map(
    [...relsXml.matchAll(/Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"/g)].map((match) => [match[1], match[2].replace(/^\//, "")])
  );

  const sheets = [...workbookXml.matchAll(/sheet[^>]*name="([^"]+)"[^>]*r:id="([^"]+)"/g)].map((match) => ({
    name: match[1],
    target: relationshipMap.get(match[2]) ?? ""
  }));

  const manifest: SheetManifest[] = sheets.map((sheet) => {
    const rows = extractRows(entries.get(sheet.target)?.toString("utf8") ?? "", sharedStrings);
    const headers = rows[0] ?? [];
    return {
      name: sheet.name,
      rows: rows.slice(1).map((row) =>
        Object.fromEntries(headers.map((header, index) => [header || `col_${index + 1}`, row[index] ?? ""]))
      )
    };
  });

  await writeFile(
    join(resultsDir, "workbook-manifest.json"),
    JSON.stringify(
      {
        generatedAt: new Date().toISOString(),
        sheets: manifest
      },
      null,
      2
    ),
    "utf8"
  );

  process.stdout.write(`Workbook manifest written to ${join(resultsDir, "workbook-manifest.json")}\n`);
}

main().catch((error) => {
  process.stderr.write(`${String(error)}\n`);
  process.exitCode = 1;
});

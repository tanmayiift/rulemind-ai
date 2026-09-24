// Every button that shows only an icon must have an accessible name (aria-label / aria-labelledby /
// title). Nine did not -- step move/remove on Policies and Scorecards, and four deletes -- so a
// screen reader announced "button, button, button" beside each policy step. Parsed with the
// TypeScript compiler rather than a regex, because attributes hold arrow functions (`=>`).
import { describe, expect, it } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import ts from "typescript";

const WEB = path.resolve(__dirname, "..");

function unnamedIconButtons(): string[] {
  const found: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (["node_modules", ".next", ".next-e2e", "test"].includes(entry.name)) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (full.endsWith(".tsx")) scan(full);
    }
  };
  const scan = (file: string) => {
    const sf = ts.createSourceFile(file, fs.readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const visit = (node: ts.Node) => {
      if (ts.isJsxElement(node)) {
        const tag = node.openingElement.tagName.getText(sf);
        if (tag === "button" || tag === "Button") {
          const attrs = node.openingElement.attributes.properties.map((a) => (a.name ? a.name.getText(sf) : ""));
          const named = attrs.some((a) => ["aria-label", "aria-labelledby", "title"].includes(a));
          const kids = node.children.filter((c) => !(ts.isJsxText(c) && c.getText(sf).trim() === ""));
          const iconOnly = kids.length > 0 && kids.every((c) => ts.isJsxSelfClosingElement(c) && /^[A-Z]/.test(c.tagName.getText(sf)));
          if (!named && iconOnly) {
            found.push(path.relative(WEB, file) + ":" + (sf.getLineAndCharacterOfPosition(node.getStart()).line + 1));
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  };
  walk(path.join(WEB, "app"));
  walk(path.join(WEB, "src"));
  return found;
}

describe("accessibility", () => {
  it("every icon-only button has an accessible name", () => {
    expect(unnamedIconButtons()).toEqual([]);
  });
});

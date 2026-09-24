import { describe, expect, it } from "vitest";
import { safeNext } from "../apps/web/src/lib/navigation";

// `?next=` comes from the URL, so it is attacker-controllable: after sign-in only a same-site
// path may be followed, never another origin.
describe("safeNext", () => {
  it("follows a relative path the auth gate sent us from", () => {
    expect(safeNext("?next=%2Fpolicies%3Fx%3D1")).toBe("/policies?x=1");
  });
  it("refuses other origins and protocol-relative URLs", () => {
    expect(safeNext("?next=https%3A%2F%2Fevil.example")).toBe("/");
    expect(safeNext("?next=%2F%2Fevil.example")).toBe("/");
    expect(safeNext("?next=%2F%5Cevil.example")).toBe("/");
    expect(safeNext("?next=javascript%3Aalert(1)")).toBe("/");
  });
  it("never loops back to the sign-in page, and defaults home", () => {
    expect(safeNext("?next=%2Flogin")).toBe("/");
    expect(safeNext("")).toBe("/");
  });
});

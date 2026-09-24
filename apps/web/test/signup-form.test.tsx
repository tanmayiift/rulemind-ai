// @vitest-environment jsdom
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

import SignupPage from "../app/signup/page";

afterEach(cleanup);

const fill = (label: RegExp | string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("signup form", () => {
  it("stays disabled until company, a well-formed email and a 12-character password are given", () => {
    render(<SignupPage />);
    const submit = screen.getByRole("button", { name: "Create workspace" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fill("Company", "Acme Lending");
    fill("Work email", "not-an-email");
    fill(/Password/, "correct horse battery");
    expect(submit.disabled).toBe(true);
    fill("Work email", "risk@acme.example");
    fill(/Password/, "short");
    expect(submit.disabled).toBe(true);
    expect(screen.getByText("7 more characters.")).toBeTruthy();
    fill(/Password/, "eleven char");
    expect(screen.getByText("1 more character.")).toBeTruthy();
    fill(/Password/, "correct horse battery");
    expect(submit.disabled).toBe(false);
  });
});

// @vitest-environment jsdom
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const apiJson = vi.fn();
vi.mock("../src/lib/api", () => ({ apiJson: (...args: unknown[]) => apiJson(...args) }));

import { usePromotionReview } from "../src/v3/PromotionReview";

afterEach(() => { cleanup(); apiJson.mockReset(); });

function Harness(props: { onPromoted: () => void }) {
  const promotion = usePromotionReview({ onPromoted: props.onPromoted, onNotify: () => undefined });
  return (
    <>
      <button onClick={() => promotion.review("rule", "r1", "Gate rule", "dev")}>Promote</button>
      {promotion.dialog}
    </>
  );
}

describe("reviewed promotion", () => {
  it("cannot be confirmed without a reason of at least ten characters", () => {
    render(<Harness onPromoted={() => undefined} />);
    fireEvent.click(screen.getByText("Promote"));
    const confirm = screen.getByTestId("confirm-promotion") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("promotion-reason"), { target: { value: "too short" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("promotion-reason"), { target: { value: "UAT sign-off RISK-12" } });
    expect(confirm.disabled).toBe(false);
  });

  it("records the written reason and never a client-chosen actor", async () => {
    apiJson.mockResolvedValue({});
    const onPromoted = vi.fn();
    render(<Harness onPromoted={onPromoted} />);
    fireEvent.click(screen.getByText("Promote"));
    fireEvent.change(screen.getByTestId("promotion-reason"), { target: { value: "UAT sign-off RISK-12" } });
    fireEvent.click(screen.getByTestId("confirm-promotion"));
    await waitFor(() => expect(onPromoted).toHaveBeenCalledOnce());
    const [, path, init] = apiJson.mock.calls[0] as [string, string, { body: string }];
    expect(path).toBe("/api/v1/rules/r1/promote");
    const body = JSON.parse(init.body);
    expect(body.reason).toBe("UAT sign-off RISK-12");
    expect(body).not.toHaveProperty("promoted_by");
  });

  it("targets the next environment", () => {
    render(<Harness onPromoted={() => undefined} />);
    fireEvent.click(screen.getByText("Promote"));
    expect(screen.getByRole("dialog", { name: "Promote Gate rule to UAT" })).toBeTruthy();
  });
});

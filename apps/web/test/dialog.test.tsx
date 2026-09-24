// @vitest-environment jsdom
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Dialog } from "../src/v3/ui";

afterEach(cleanup);

function Harness(props: { busy?: boolean; onClose?: () => void }) {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <Dialog open={open} title="Promote Probe" busy={props.busy}
              onClose={() => { props.onClose?.(); setOpen(false); }}
              footer={<><button>Cancel</button><button>Confirm</button></>}>
        <textarea aria-label="Reason" />
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("is a modal dialog named by its title, and focuses its first field", () => {
    render(<Harness />);
    fireEvent.click(screen.getByText("Open"));
    const dialog = screen.getByRole("dialog", { name: "Promote Probe" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(screen.getByLabelText("Reason"));
  });

  it("closes on Escape and returns focus to what opened it", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const opener = screen.getByText("Open");
    opener.focus();
    fireEvent.click(opener);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("ignores Escape while busy, so a request in flight is not orphaned", () => {
    const onClose = vi.fn();
    render(<Harness busy onClose={onClose} />);
    fireEvent.click(screen.getByText("Open"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps Tab inside the dialog", () => {
    render(<Harness />);
    fireEvent.click(screen.getByText("Open"));
    const confirm = screen.getByText("Confirm");
    confirm.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByLabelText("Reason"));
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(confirm);
  });
});

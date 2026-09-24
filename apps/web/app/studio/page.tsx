import { redirect } from "next/navigation";

// Build Studio has been merged into Workflow Builder (2026-09-24).
//
// It was written as "a single canvas-first surface that replaces the split Policies-step-list /
// Workflow-Builder editors", shipped, and the editor it replaced was never removed -- so the
// product carried two canvases over the same policy entity, each missing things the other had.
// Everything Studio could do now lives in /workflow-builder: creating and renaming a policy,
// inline rule authoring, keyboard reorder, the review-gate settings, the environment label on the
// picker, and the late-load guard. Two things were deliberately NOT carried over because they did
// not work: Studio's branch "condition" (the executor never reads it, so a branch built here routed
// nothing) and its loop editor (no way to add a body, so a loop iterated nothing).
//
// A real redirect rather than deleting the route, so bookmarks, onboarding links and anyone's
// muscle memory still land on a working canvas instead of a 404.
export default function StudioRedirect(): never {
  redirect("/workflow-builder");
}

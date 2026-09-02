# `@rulemind/sdk`

A compatibility alias. This package re-exports
[`@rulemind/sdk-js`](../sdk-js) in full and adds nothing of its own:

```ts
export * from "@rulemind/sdk-js";
```

**Use `@rulemind/sdk-js` directly in new code.** This alias exists so that
existing imports of `@rulemind/sdk` keep working; it is not a separate
implementation and has no independent API surface or version policy.

See [`../sdk-js/README.md`](../sdk-js/README.md) for installation, usage and the
edge-vs-server evaluation contract.

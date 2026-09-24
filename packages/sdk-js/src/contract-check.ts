/**
 * Compile-time only: the response fields this SDK hands to integrators, read through the types
 * generated from the API's contract. The client passes those types through generics, so without
 * this file a field the API dropped or retyped would still compile. With it, `tsc` fails here.
 * Nothing is emitted at runtime.
 */
import type { RuleResult, SdkBundleResponse, SdkDecideResponse } from "./index";

type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
type Assert<T extends true> = T;

export type SdkContract = [
  Assert<Same<SdkDecideResponse["outcome"], string>>,
  Assert<Same<SdkDecideResponse["score"], number | null>>,
  Assert<Same<SdkDecideResponse["ruleResults"], RuleResult[]>>,
  Assert<Same<SdkDecideResponse["latencyMs"], number>>,
  Assert<Same<SdkDecideResponse["requestId"], string | null>>,
  Assert<Same<SdkDecideResponse["serverOnlyStepsSkipped"], string[]>>,
  Assert<Same<RuleResult["rule_id"], string>>,
  Assert<Same<RuleResult["passed"], boolean>>,
  Assert<Same<RuleResult["outcome"], string>>,
  Assert<Same<SdkBundleResponse["bundleVersion"], number>>,
  Assert<Same<SdkBundleResponse["encryptedBundle"], string>>,
  Assert<Same<SdkBundleResponse["encryptedKey"], string | null>>,
  Assert<Same<SdkBundleResponse["signature"], string>>,
  Assert<Same<SdkBundleResponse["checksum"], string>>,
];

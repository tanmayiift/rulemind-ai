import 'package:flutter/material.dart';

import 'theme/tokens.dart';

/// One input field on a journey form.
class JourneyField {
  const JourneyField({
    required this.key,
    required this.label,
    required this.min,
    required this.max,
    this.isToggle = false,
    this.help,
    this.suffix,
  });

  final String key;
  final String label;
  final num min;
  final num max;
  final bool isToggle;
  final String? help;
  final String? suffix;
}

/// A named preset that demonstrates one decision path end-to-end.
class Scenario {
  const Scenario({
    required this.name,
    required this.blurb,
    required this.values,
    required this.expected,
  });

  final String name;
  final String blurb;
  final Map<String, num> values;

  /// What the policy SHOULD return — the app flags any divergence, which is how
  /// this demo doubles as a conformance check against the live engine.
  final String expected;
}

class Journey {
  const Journey({
    required this.id,
    required this.policyId,
    required this.sourceId,
    required this.title,
    required this.sector,
    required this.blurb,
    required this.icon,
    required this.identity,
    required this.fields,
    required this.scenarios,
  });

  final String id;
  final String policyId;
  final String sourceId;
  final String title;
  final String sector;
  final String blurb;
  final IconData icon;
  /// Visual identity. Deliberately an [RmIdentity], not a [Color]: it is legal
  /// only as a glyph tint and a header rail, and the type makes it incapable of
  /// filling a container or colouring a paragraph.
  final RmIdentity identity;
  final List<JourneyField> fields;
  final List<Scenario> scenarios;

  Map<String, dynamic> payloadFor(Map<String, num> values) => {
        sourceId: {for (final e in values.entries) e.key: e.value},
      };
}

const kJourneys = <Journey>[
  // ───────────────────────────── INSURANCE ─────────────────────────────
  Journey(
    id: 'claims',
    policyId: 'policy_claim_triage',
    sourceId: 'claim',
    title: 'Motor Claim Triage',
    sector: 'Insurance',
    blurb: 'Route a first notice of loss to fast-track settlement, investigation, or decline.',
    icon: Icons.car_crash_outlined,
    identity: RmIdentity.claims,
    fields: [
      JourneyField(key: 'claim_amount_inr', label: 'Claim amount', min: 0, max: 600000, suffix: '₹'),
      JourneyField(key: 'policy_age_months', label: 'Policy age', min: 0, max: 120, suffix: 'mo'),
      JourneyField(key: 'prior_claims_12m', label: 'Prior claims (12m)', min: 0, max: 8),
      JourneyField(key: 'incident_severity', label: 'Incident severity', min: 1, max: 5),
      JourneyField(key: 'claimant_tenure_days', label: 'Claimant tenure', min: 0, max: 2000, suffix: 'd'),
      JourneyField(key: 'documents_complete', label: 'Documents complete', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'police_report_filed', label: 'Police report filed', min: 0, max: 1, isToggle: true),
    ],
    scenarios: [
      Scenario(
        name: 'Clean low-value claim',
        blurb: 'Long-tenured customer, full documents, no claim history.',
        expected: 'approve',
        values: {
          'claim_amount_inr': 28000, 'policy_age_months': 30, 'prior_claims_12m': 0,
          'documents_complete': 1, 'police_report_filed': 1, 'incident_severity': 2,
          'claimant_tenure_days': 900,
        },
      ),
      Scenario(
        name: 'Missing documents',
        blurb: 'Same customer, incomplete paperwork — should hold for review.',
        expected: 'review',
        values: {
          'claim_amount_inr': 28000, 'policy_age_months': 30, 'prior_claims_12m': 0,
          'documents_complete': 0, 'police_report_filed': 1, 'incident_severity': 2,
          'claimant_tenure_days': 900,
        },
      ),
      Scenario(
        name: 'Serial claimant, new policy',
        blurb: 'Four claims in 12 months on a one-month-old policy.',
        expected: 'reject',
        values: {
          'claim_amount_inr': 90000, 'policy_age_months': 1, 'prior_claims_12m': 4,
          'documents_complete': 1, 'police_report_filed': 0, 'incident_severity': 4,
          'claimant_tenure_days': 20,
        },
      ),
      Scenario(
        name: 'High-value referral',
        blurb: 'Everything checks out, but the amount exceeds the auto-settle ceiling.',
        expected: 'review',
        values: {
          'claim_amount_inr': 450000, 'policy_age_months': 40, 'prior_claims_12m': 0,
          'documents_complete': 1, 'police_report_filed': 1, 'incident_severity': 3,
          'claimant_tenure_days': 1200,
        },
      ),
    ],
  ),

  // ──────────────────────────── E-COMMERCE ────────────────────────────
  Journey(
    id: 'checkout',
    policyId: 'policy_checkout_fraud',
    sourceId: 'checkout',
    title: 'Checkout Fraud Screening',
    sector: 'E-commerce',
    blurb: 'Approve, step up to 3-D Secure, or block an order in real time at checkout.',
    icon: Icons.shopping_cart_checkout,
    identity: RmIdentity.checkout,
    fields: [
      JourneyField(key: 'order_value_inr', label: 'Order value', min: 0, max: 100000, suffix: '₹'),
      JourneyField(key: 'account_age_days', label: 'Account age', min: 0, max: 1500, suffix: 'd'),
      JourneyField(key: 'device_risk_score', label: 'Device risk', min: 0, max: 100),
      JourneyField(key: 'orders_last_1h', label: 'Orders last 1h', min: 0, max: 15),
      JourneyField(key: 'chargebacks_lifetime', label: 'Chargebacks', min: 0, max: 8),
      JourneyField(key: 'billing_shipping_match', label: 'Billing = shipping', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'payment_method_new', label: 'New payment method', min: 0, max: 1, isToggle: true),
    ],
    scenarios: [
      Scenario(
        name: 'Trusted repeat buyer',
        blurb: 'Two-year-old account, low-risk device, address match.',
        expected: 'approve',
        values: {
          'order_value_inr': 3200, 'account_age_days': 800, 'device_risk_score': 8,
          'billing_shipping_match': 1, 'orders_last_1h': 1, 'chargebacks_lifetime': 0,
          'payment_method_new': 0,
        },
      ),
      Scenario(
        name: 'Chargeback abuser',
        blurb: 'Good signals everywhere, but five lifetime chargebacks — hard block.',
        expected: 'reject',
        values: {
          'order_value_inr': 3200, 'account_age_days': 800, 'device_risk_score': 8,
          'billing_shipping_match': 1, 'orders_last_1h': 1, 'chargebacks_lifetime': 5,
          'payment_method_new': 0,
        },
      ),
      Scenario(
        name: 'Card-testing velocity',
        blurb: 'Nine small orders in an hour from a brand-new high-risk device.',
        expected: 'review',
        values: {
          'order_value_inr': 900, 'account_age_days': 2, 'device_risk_score': 85,
          'billing_shipping_match': 0, 'orders_last_1h': 9, 'chargebacks_lifetime': 0,
          'payment_method_new': 1,
        },
      ),
      Scenario(
        name: 'High-value, new account',
        blurb: 'Large basket, three-day-old account, mismatched address.',
        expected: 'review',
        values: {
          'order_value_inr': 62000, 'account_age_days': 3, 'device_risk_score': 75,
          'billing_shipping_match': 0, 'orders_last_1h': 1, 'chargebacks_lifetime': 0,
          'payment_method_new': 1,
        },
      ),
    ],
  ),

  // ─────────────────────────── MARKETPLACE ───────────────────────────
  Journey(
    id: 'seller',
    policyId: 'policy_seller_onboarding',
    sourceId: 'seller',
    title: 'Seller Onboarding',
    sector: 'Marketplace',
    blurb: 'Activate a new seller, send to manual KYB review, or reject on sanctions.',
    icon: Icons.storefront_outlined,
    identity: RmIdentity.seller,
    fields: [
      JourneyField(key: 'business_vintage_months', label: 'Business vintage', min: 0, max: 120, suffix: 'mo'),
      JourneyField(key: 'category_risk_tier', label: 'Category risk tier', min: 1, max: 5),
      JourneyField(key: 'gst_valid', label: 'GST valid', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'pan_verified', label: 'PAN verified', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'bank_penny_drop_ok', label: 'Bank penny-drop OK', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'director_kyc_complete', label: 'Director KYC done', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'sanctions_hit', label: 'Sanctions hit', min: 0, max: 1, isToggle: true),
    ],
    scenarios: [
      Scenario(
        name: 'Verified, established',
        blurb: 'Four-year-old business, every check green, low-risk category.',
        expected: 'approve',
        values: {
          'gst_valid': 1, 'pan_verified': 1, 'bank_penny_drop_ok': 1, 'sanctions_hit': 0,
          'business_vintage_months': 48, 'category_risk_tier': 1, 'director_kyc_complete': 1,
        },
      ),
      Scenario(
        name: 'Sanctions hit',
        blurb: 'Otherwise perfect profile — sanctions screening overrides everything.',
        expected: 'reject',
        values: {
          'gst_valid': 1, 'pan_verified': 1, 'bank_penny_drop_ok': 1, 'sanctions_hit': 1,
          'business_vintage_months': 48, 'category_risk_tier': 1, 'director_kyc_complete': 1,
        },
      ),
      Scenario(
        name: 'Incomplete KYB',
        blurb: 'No GST, failed penny-drop, director KYC pending.',
        expected: 'review',
        values: {
          'gst_valid': 0, 'pan_verified': 1, 'bank_penny_drop_ok': 0, 'sanctions_hit': 0,
          'business_vintage_months': 4, 'category_risk_tier': 2, 'director_kyc_complete': 0,
        },
      ),
      Scenario(
        name: 'High-risk category',
        blurb: 'Fully verified, but selling in a tier-4 restricted category.',
        expected: 'review',
        values: {
          'gst_valid': 1, 'pan_verified': 1, 'bank_penny_drop_ok': 1, 'sanctions_hit': 0,
          'business_vintage_months': 36, 'category_risk_tier': 4, 'director_kyc_complete': 1,
        },
      ),
    ],
  ),

  // ───────────────────────────── TELECOM ─────────────────────────────
  Journey(
    id: 'telecom',
    policyId: 'policy_telecom_postpaid',
    sourceId: 'telecom',
    title: 'Postpaid Eligibility',
    sector: 'Telecom',
    blurb: 'Decide whether a subscriber qualifies for a postpaid plan without a deposit.',
    icon: Icons.sim_card_outlined,
    identity: RmIdentity.telecom,
    fields: [
      JourneyField(key: 'tenure_months', label: 'Tenure', min: 0, max: 120, suffix: 'mo'),
      JourneyField(key: 'avg_monthly_spend_inr', label: 'Avg monthly spend', min: 0, max: 3000, suffix: '₹'),
      JourneyField(key: 'late_payments_6m', label: 'Late payments (6m)', min: 0, max: 6),
      JourneyField(key: 'existing_lines', label: 'Existing lines', min: 0, max: 12),
      JourneyField(key: 'credit_bureau_band', label: 'Bureau band', min: 1, max: 5),
      JourneyField(key: 'kyc_complete', label: 'KYC complete', min: 0, max: 1, isToggle: true),
      JourneyField(key: 'port_in_request', label: 'Port-in request', min: 0, max: 1, isToggle: true),
    ],
    scenarios: [
      Scenario(
        name: 'Loyal high spender',
        blurb: 'Five years on network, never late, top bureau band.',
        expected: 'approve',
        values: {
          'tenure_months': 60, 'avg_monthly_spend_inr': 1500, 'late_payments_6m': 0,
          'kyc_complete': 1, 'port_in_request': 0, 'existing_lines': 2, 'credit_bureau_band': 1,
        },
      ),
      Scenario(
        name: 'KYC incomplete',
        blurb: 'Excellent customer, but regulatory KYC is a hard gate.',
        expected: 'reject',
        values: {
          'tenure_months': 60, 'avg_monthly_spend_inr': 1500, 'late_payments_6m': 0,
          'kyc_complete': 0, 'port_in_request': 0, 'existing_lines': 2, 'credit_bureau_band': 1,
        },
      ),
      Scenario(
        name: 'Chronic late payer',
        blurb: 'Four late payments in six months, low spend, weak bureau band.',
        expected: 'review',
        values: {
          'tenure_months': 8, 'avg_monthly_spend_inr': 250, 'late_payments_6m': 4,
          'kyc_complete': 1, 'port_in_request': 1, 'existing_lines': 1, 'credit_bureau_band': 3,
        },
      ),
      Scenario(
        name: 'Line limit exceeded',
        blurb: 'Strong profile already holding nine lines — needs a human.',
        expected: 'review',
        values: {
          'tenure_months': 30, 'avg_monthly_spend_inr': 900, 'late_payments_6m': 0,
          'kyc_complete': 1, 'port_in_request': 0, 'existing_lines': 9, 'credit_bureau_band': 2,
        },
      ),
    ],
  ),
];

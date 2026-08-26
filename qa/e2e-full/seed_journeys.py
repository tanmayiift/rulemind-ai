"""Seed four NON-LENDING decisioning journeys used by the Flutter demo app + E2E suites.

Insurance claims · e-commerce fraud · marketplace seller KYC · telecom postpaid.
Idempotent: re-running replaces the same ids.
"""
import json, os, sys, httpx

BASE = os.getenv("RM_BASE", "http://127.0.0.1:8080")
KEY  = os.getenv("RM_KEY", "rm_live_devlocaltenantkey000000000000")
c = httpx.Client(base_url=BASE, headers={"x-api-key": KEY, "content-type": "application/json"}, timeout=60)

# POST /api/v1/<kind> IGNORES a client-supplied `id` and derives one by slugifying
# `name` (verified: id "claim" + name "Motor Claim FNOL" -> "motor_claim_fnol").
# PUT honours the path id, so try PUT first; on 404 POST and adopt whatever id the
# server assigns, rewriting downstream references through ID_MAP.
ID_MAP = {}

def upsert(kind, obj):
    oid = obj["id"]
    r = c.put(f"/api/v1/{kind}/{oid}", json=obj)
    if r.status_code in (200, 201):
        ID_MAP[oid] = oid
        return "updated"
    r = c.post(f"/api/v1/{kind}", json=obj)
    if r.status_code in (200, 201):
        real = r.json().get("id", oid)
        ID_MAP[oid] = real
        return "created" if real == oid else f"created(id->{real})"
    return f"FAIL {r.status_code}: {r.text[:180]}"

_REF_KEYS = ("source_id", "variable", "variable_id", "ref_id")

def remap(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _REF_KEYS and isinstance(v, str):
                out[k] = ID_MAP.get(v, v)
            else:
                out[k] = remap(v)
        return out
    if isinstance(obj, list):
        return [remap(x) for x in obj]
    return obj

def var(vid, name, source, category, expr):
    return {"id": vid, "name": name, "source_id": source, "category": category,
            "code": f'@variable(source="{source}")\ndef {vid}(payload, variables, apis):\n    {expr}\n',
            "description": name, "status": "prod"}

def cond(cid, variable, op, value):
    return {"type": "condition", "id": cid, "variable": variable, "operator": op, "value": value}

def rule(rid, name, conds, logic="AND", on_pass="approve", on_fail="reject"):
    return {"id": rid, "name": name, "nodes": [], "rule_format": "v1", "status": "prod",
            "tree": {"type": "group", "id": rid + "_root", "logic": logic,
                     "children": conds, "onPass": on_pass, "onFail": on_fail}}

JOURNEYS = []

# ───────────────────────── 1. INSURANCE CLAIM TRIAGE ─────────────────────────
JOURNEYS.append(dict(
  connector={"id":"claim","name":"Claim","icon":"car_crash","color":"#C2410C",
    "description":"First notice of loss: incident, documents, claim history",
    "schema_paths":["claim_amount_inr","policy_age_months","prior_claims_12m","documents_complete",
                    "police_report_filed","incident_severity","claimant_tenure_days"],
    "sample_payload":{"claim_amount_inr":38000,"policy_age_months":26,"prior_claims_12m":0,
                      "documents_complete":1,"police_report_filed":1,"incident_severity":2,
                      "claimant_tenure_days":780},
    "is_active":True,"config":{"auth_type":"signed_webhook","base_url":"rulemind://simulate/claims","timeout_ms":1500,"retries":0}},
  variables=[
    var("claim_amount_inr","Claim Amount (INR)","claim","Claims",'return int(payload.get("claim_amount_inr", 0))'),
    var("claim_policy_age_months","Policy Age (Months)","claim","Claims",'return int(payload.get("policy_age_months", 0))'),
    var("claim_prior_claims_12m","Prior Claims (12m)","claim","Claims",'return int(payload.get("prior_claims_12m", 0))'),
    var("claim_documents_complete","Documents Complete","claim","Claims",'return int(payload.get("documents_complete", 0))'),
    var("claim_police_report","Police Report Filed","claim","Claims",'return int(payload.get("police_report_filed", 0))'),
    var("claim_severity","Incident Severity","claim","Claims",'return int(payload.get("incident_severity", 1))'),
    var("claim_tenure_days","Claimant Tenure (Days)","claim","Claims",'return int(payload.get("claimant_tenure_days", 0))'),
  ],
  rules=[
    rule("rule_claim_docs_gate","Claim Documentation Gate",
         [cond("c_docs","claim_documents_complete","==",1)], on_pass="approve", on_fail="review"),
    rule("rule_claim_fraud_gate","Claim Fraud Signal Gate",
         [cond("c_prior","claim_prior_claims_12m","<=",2),
          cond("c_page","claim_policy_age_months",">=",3)], on_pass="approve", on_fail="reject"),
    rule("rule_claim_highvalue_gate","High-Value Claim Referral",
         [cond("c_amt","claim_amount_inr","<=",200000)], on_pass="approve", on_fail="review"),
  ],
  scorecard={"id":"sc_claim_confidence","name":"Claim Confidence Score","base_score":500,"max_score":900,"status":"prod",
    "bins":[
      {"variable_id":"claim_prior_claims_12m","ranges":[{"min":0,"max":0,"points":90},{"min":1,"max":2,"points":20},{"min":3,"max":99,"points":-120}]},
      {"variable_id":"claim_policy_age_months","ranges":[{"min":0,"max":2,"points":-90},{"min":3,"max":11,"points":10},{"min":12,"max":999,"points":80}]},
      {"variable_id":"claim_amount_inr","ranges":[{"min":0,"max":49999,"points":80},{"min":50000,"max":199999,"points":20},{"min":200000,"max":99999999,"points":-70}]},
      {"variable_id":"claim_tenure_days","ranges":[{"min":0,"max":89,"points":-60},{"min":90,"max":364,"points":20},{"min":365,"max":99999,"points":70}]},
    ]},
  policy={"id":"policy_claim_triage","name":"Motor Claim Triage","status":"prod","trigger":None,
    "steps":[
      {"id":"claim_connector","type":"connector","ref_id":"claim","label":"Claim FNOL"},
      {"id":"claim_docs","type":"rule","ref_id":"rule_claim_docs_gate","label":"Documentation"},
      {"id":"claim_fraud","type":"rule","ref_id":"rule_claim_fraud_gate","label":"Fraud Signals"},
      {"id":"claim_score","type":"scorecard","ref_id":"sc_claim_confidence","label":"Claim Confidence"},
      {"id":"claim_value","type":"rule","ref_id":"rule_claim_highvalue_gate","label":"High-Value Referral"},
      {"id":"claim_outcome","type":"outcome","ref_id":"approve","label":"Fast-Track Settlement",
       "config":{"condition":"outcome != 'reject' and outcome != 'review'"}},
    ]}))

# ───────────────────────── 2. E-COMMERCE ORDER FRAUD ─────────────────────────
JOURNEYS.append(dict(
  connector={"id":"checkout","name":"Checkout","icon":"shopping_cart","color":"#7C3AED",
    "description":"Order, account, device and velocity signals at checkout",
    "schema_paths":["order_value_inr","account_age_days","device_risk_score","billing_shipping_match",
                    "orders_last_1h","chargebacks_lifetime","payment_method_new"],
    "sample_payload":{"order_value_inr":7400,"account_age_days":420,"device_risk_score":12,
                      "billing_shipping_match":1,"orders_last_1h":1,"chargebacks_lifetime":0,"payment_method_new":0},
    "is_active":True,"config":{"auth_type":"signed_webhook","base_url":"rulemind://simulate/checkout","timeout_ms":800,"retries":0}},
  variables=[
    var("ck_order_value_inr","Order Value (INR)","checkout","Fraud",'return int(payload.get("order_value_inr", 0))'),
    var("ck_account_age_days","Account Age (Days)","checkout","Fraud",'return int(payload.get("account_age_days", 0))'),
    var("ck_device_risk","Device Risk Score","checkout","Fraud",'return int(payload.get("device_risk_score", 0))'),
    var("ck_addr_match","Billing/Shipping Match","checkout","Fraud",'return int(payload.get("billing_shipping_match", 0))'),
    var("ck_velocity_1h","Orders Last 1h","checkout","Fraud",'return int(payload.get("orders_last_1h", 0))'),
    var("ck_chargebacks","Chargebacks Lifetime","checkout","Fraud",'return int(payload.get("chargebacks_lifetime", 0))'),
    var("ck_new_payment","New Payment Method","checkout","Fraud",'return int(payload.get("payment_method_new", 0))'),
  ],
  rules=[
    rule("rule_ck_chargeback_block","Chargeback Hard Block",
         [cond("c_cb","ck_chargebacks","<=",1)], on_pass="approve", on_fail="reject"),
    rule("rule_ck_velocity_gate","Velocity Gate",
         [cond("c_vel","ck_velocity_1h","<=",4)], on_pass="approve", on_fail="review"),
    rule("rule_ck_device_gate","Device & Address Gate",
         [cond("c_dev","ck_device_risk","<=",60),
          cond("c_addr","ck_addr_match","==",1)], on_pass="approve", on_fail="review"),
  ],
  scorecard={"id":"sc_checkout_trust","name":"Checkout Trust Score","base_score":500,"max_score":900,"status":"prod",
    "bins":[
      {"variable_id":"ck_account_age_days","ranges":[{"min":0,"max":6,"points":-110},{"min":7,"max":89,"points":-20},{"min":90,"max":364,"points":50},{"min":365,"max":99999,"points":100}]},
      {"variable_id":"ck_device_risk","ranges":[{"min":0,"max":20,"points":90},{"min":21,"max":60,"points":10},{"min":61,"max":100,"points":-130}]},
      {"variable_id":"ck_order_value_inr","ranges":[{"min":0,"max":4999,"points":60},{"min":5000,"max":49999,"points":10},{"min":50000,"max":99999999,"points":-60}]},
      {"variable_id":"ck_velocity_1h","ranges":[{"min":0,"max":2,"points":60},{"min":3,"max":5,"points":-20},{"min":6,"max":999,"points":-120}]},
    ]},
  policy={"id":"policy_checkout_fraud","name":"Checkout Fraud Screening","status":"prod","trigger":None,
    "steps":[
      {"id":"ck_connector","type":"connector","ref_id":"checkout","label":"Checkout Signals"},
      {"id":"ck_cb","type":"rule","ref_id":"rule_ck_chargeback_block","label":"Chargeback Block"},
      {"id":"ck_vel","type":"rule","ref_id":"rule_ck_velocity_gate","label":"Velocity"},
      {"id":"ck_score","type":"scorecard","ref_id":"sc_checkout_trust","label":"Trust Score"},
      {"id":"ck_dev","type":"rule","ref_id":"rule_ck_device_gate","label":"Device & Address"},
      {"id":"ck_outcome","type":"outcome","ref_id":"approve","label":"Order Approved",
       "config":{"condition":"outcome != 'reject' and outcome != 'review'"}},
    ]}))

# ──────────────────── 3. MARKETPLACE SELLER ONBOARDING (KYB) ────────────────────
JOURNEYS.append(dict(
  connector={"id":"seller","name":"Seller","icon":"storefront","color":"#0F766E",
    "description":"Business verification, sanctions, category risk and vintage",
    "schema_paths":["gst_valid","pan_verified","bank_penny_drop_ok","sanctions_hit",
                    "business_vintage_months","category_risk_tier","director_kyc_complete"],
    "sample_payload":{"gst_valid":1,"pan_verified":1,"bank_penny_drop_ok":1,"sanctions_hit":0,
                      "business_vintage_months":30,"category_risk_tier":1,"director_kyc_complete":1},
    "is_active":True,"config":{"auth_type":"signed_webhook","base_url":"rulemind://simulate/seller","timeout_ms":2000,"retries":1}},
  variables=[
    var("sl_gst_valid","GST Valid","seller","Onboarding",'return int(payload.get("gst_valid", 0))'),
    var("sl_pan_verified","PAN Verified","seller","Onboarding",'return int(payload.get("pan_verified", 0))'),
    var("sl_penny_drop","Bank Penny Drop OK","seller","Onboarding",'return int(payload.get("bank_penny_drop_ok", 0))'),
    var("sl_sanctions_hit","Sanctions Hit","seller","Onboarding",'return int(payload.get("sanctions_hit", 0))'),
    var("sl_vintage_months","Business Vintage (Months)","seller","Onboarding",'return int(payload.get("business_vintage_months", 0))'),
    var("sl_category_risk","Category Risk Tier","seller","Onboarding",'return int(payload.get("category_risk_tier", 1))'),
    var("sl_director_kyc","Director KYC Complete","seller","Onboarding",'return int(payload.get("director_kyc_complete", 0))'),
  ],
  rules=[
    rule("rule_sl_sanctions","Sanctions Screening",
         [cond("c_sanc","sl_sanctions_hit","==",0)], on_pass="approve", on_fail="reject"),
    rule("rule_sl_identity","Identity & Banking Verification",
         [cond("c_gst","sl_gst_valid","==",1),
          cond("c_pan","sl_pan_verified","==",1),
          cond("c_pd","sl_penny_drop","==",1),
          cond("c_dkyc","sl_director_kyc","==",1)], on_pass="approve", on_fail="review"),
    rule("rule_sl_category","High-Risk Category Referral",
         [cond("c_cat","sl_category_risk","<=",2)], on_pass="approve", on_fail="review"),
  ],
  scorecard={"id":"sc_seller_quality","name":"Seller Quality Score","base_score":500,"max_score":900,"status":"prod",
    "bins":[
      {"variable_id":"sl_vintage_months","ranges":[{"min":0,"max":5,"points":-100},{"min":6,"max":23,"points":20},{"min":24,"max":9999,"points":110}]},
      {"variable_id":"sl_category_risk","ranges":[{"min":1,"max":1,"points":80},{"min":2,"max":2,"points":10},{"min":3,"max":9,"points":-110}]},
      {"variable_id":"sl_gst_valid","ranges":[{"min":0,"max":0,"points":-90},{"min":1,"max":1,"points":60}]},
      {"variable_id":"sl_penny_drop","ranges":[{"min":0,"max":0,"points":-80},{"min":1,"max":1,"points":50}]},
    ]},
  policy={"id":"policy_seller_onboarding","name":"Marketplace Seller Onboarding","status":"prod","trigger":None,
    "steps":[
      {"id":"sl_connector","type":"connector","ref_id":"seller","label":"Seller KYB"},
      {"id":"sl_sanc","type":"rule","ref_id":"rule_sl_sanctions","label":"Sanctions"},
      {"id":"sl_id","type":"rule","ref_id":"rule_sl_identity","label":"Identity & Banking"},
      {"id":"sl_score","type":"scorecard","ref_id":"sc_seller_quality","label":"Seller Quality"},
      {"id":"sl_cat","type":"rule","ref_id":"rule_sl_category","label":"Category Risk"},
      {"id":"sl_outcome","type":"outcome","ref_id":"approve","label":"Seller Activated",
       "config":{"condition":"outcome != 'reject' and outcome != 'review'"}},
    ]}))

# ──────────────────── 4. TELECOM POSTPAID PLAN ELIGIBILITY ────────────────────
JOURNEYS.append(dict(
  connector={"id":"telecom","name":"Telecom","icon":"sim_card","color":"#B45309",
    "description":"Tenure, payment behaviour, KYC and porting status",
    "schema_paths":["tenure_months","avg_monthly_spend_inr","late_payments_6m","kyc_complete",
                    "port_in_request","existing_lines","credit_bureau_band"],
    "sample_payload":{"tenure_months":34,"avg_monthly_spend_inr":899,"late_payments_6m":0,
                      "kyc_complete":1,"port_in_request":0,"existing_lines":2,"credit_bureau_band":2},
    "is_active":True,"config":{"auth_type":"signed_webhook","base_url":"rulemind://simulate/telecom","timeout_ms":1200,"retries":0}},
  variables=[
    var("tc_tenure_months","Tenure (Months)","telecom","Telecom",'return int(payload.get("tenure_months", 0))'),
    var("tc_avg_spend","Avg Monthly Spend (INR)","telecom","Telecom",'return int(payload.get("avg_monthly_spend_inr", 0))'),
    var("tc_late_payments","Late Payments (6m)","telecom","Telecom",'return int(payload.get("late_payments_6m", 0))'),
    var("tc_kyc_complete","KYC Complete","telecom","Telecom",'return int(payload.get("kyc_complete", 0))'),
    var("tc_port_in","Port-In Request","telecom","Telecom",'return int(payload.get("port_in_request", 0))'),
    var("tc_existing_lines","Existing Lines","telecom","Telecom",'return int(payload.get("existing_lines", 0))'),
    var("tc_bureau_band","Credit Bureau Band","telecom","Telecom",'return int(payload.get("credit_bureau_band", 3))'),
  ],
  rules=[
    rule("rule_tc_kyc","KYC Completion Gate",
         [cond("c_kyc","tc_kyc_complete","==",1)], on_pass="approve", on_fail="reject"),
    rule("rule_tc_payment","Payment Behaviour Gate",
         [cond("c_late","tc_late_payments","<=",1)], on_pass="approve", on_fail="review"),
    rule("rule_tc_lines","Line Limit Gate",
         [cond("c_lines","tc_existing_lines","<=",5)], on_pass="approve", on_fail="review"),
  ],
  scorecard={"id":"sc_telecom_eligibility","name":"Postpaid Eligibility Score","base_score":500,"max_score":900,"status":"prod",
    "bins":[
      {"variable_id":"tc_tenure_months","ranges":[{"min":0,"max":5,"points":-90},{"min":6,"max":23,"points":30},{"min":24,"max":9999,"points":100}]},
      {"variable_id":"tc_late_payments","ranges":[{"min":0,"max":0,"points":90},{"min":1,"max":2,"points":-20},{"min":3,"max":99,"points":-130}]},
      {"variable_id":"tc_avg_spend","ranges":[{"min":0,"max":299,"points":-40},{"min":300,"max":799,"points":30},{"min":800,"max":9999999,"points":90}]},
      {"variable_id":"tc_bureau_band","ranges":[{"min":1,"max":1,"points":90},{"min":2,"max":2,"points":40},{"min":3,"max":9,"points":-80}]},
    ]},
  policy={"id":"policy_telecom_postpaid","name":"Telecom Postpaid Eligibility","status":"prod","trigger":None,
    "steps":[
      {"id":"tc_connector","type":"connector","ref_id":"telecom","label":"Subscriber Profile"},
      {"id":"tc_kyc","type":"rule","ref_id":"rule_tc_kyc","label":"KYC"},
      {"id":"tc_pay","type":"rule","ref_id":"rule_tc_payment","label":"Payment Behaviour"},
      {"id":"tc_score","type":"scorecard","ref_id":"sc_telecom_eligibility","label":"Eligibility Score"},
      {"id":"tc_lines","type":"rule","ref_id":"rule_tc_lines","label":"Line Limit"},
      {"id":"tc_outcome","type":"outcome","ref_id":"approve","label":"Postpaid Approved",
       "config":{"condition":"outcome != 'reject' and outcome != 'review'"}},
    ]}))

if __name__ == "__main__":
    fails = 0
    for j in JOURNEYS:
        print(f"\n── {j['policy']['name']}")
        for kind, items in (("connectors",[j["connector"]]), ("variables",j["variables"]),
                            ("rules",j["rules"]), ("scorecards",[j["scorecard"]]), ("policies",[j["policy"]])):
            for obj in items:
                res = upsert(kind, remap(obj))
                if res.startswith("FAIL"): fails += 1; print(f"   ❌ {kind}/{obj['id']}: {res}")
        print(f"   ✓ {len(j['variables'])} vars, {len(j['rules'])} rules, 1 scorecard, 1 policy")
    print(f"\n{'FAILURES: '+str(fails) if fails else 'ALL JOURNEYS SEEDED OK'}")
    sys.exit(1 if fails else 0)

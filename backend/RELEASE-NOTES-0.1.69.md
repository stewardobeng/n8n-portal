# PORTAL 0.1.69: plan-aware payment modal + real package feature lists.

## 1) BUG FIX — payment confirmation modal ignored the picked plan
The modal hard-read the first active plan, so "Subscribe Plus (GHS 500)"
displayed "GHS 300 per year" in the confirmation banner. `chosenPlan` was
already set by the button click (line 2377) and sent to /checkout correctly;
only the modal display was wrong.

Before (paymentModal, app.js ~1499):
    var plan = state.plans.find(function (p) { return p.active; }) || {};

After:
    var plan = state.plans.find(function (p) {
      return p.active && ((chosenPlan === "b") === (p.amount_minor === 50000));
    }) || state.plans.find(function (p) { return p.active; }) || {};

Also the banner subtitle now says which package: "n8n Workspace (GHS 300)"
vs "n8n Workspace Plus (GHS 500) - includes Python code node + AI assistant
setup support".

## 2) BILLING PAGE — distinct package contents
The two plan cards previously rendered the SAME generic list
("Your own private workspace / Your own secure address / Email support /
Automatic annual renewal") for both prices.

Now (only ACTIVE cards get feature lists; disabled/coming-soon cards keep
their placeholder copy):

Plan A - "n8n Workspace" (GHS 300/yr):
  - Your own private n8n workspace
  - Your own secure address (https)
  - Full workflow builder - unlimited workflows
  - 400+ app integrations and AI model nodes
  - JavaScript code node
  - Email support
  - Automatic annual renewal

Plan B - "n8n Workspace Plus" (GHS 500/yr) - everything in n8n Workspace, plus:
  - Python code node (managed Python environment attached by our team)
  - AI assistant setup: guided configuration of your n8n AI Assistant
    with hands-on help from SteProTECH
  - Priority email support

Feature lists are per-card in planCards; the Plus card carries a short
"Everything in n8n Workspace, plus:" lead line.

## Verification
- node --check app.js: OK
- hermes verify: bootstrap ok, 220 tests pass
- Deployed 0.1.69 to 137.131.55.194 (container healthy, public 200)
- Manual UI check instructions handed to Steward (billing page + both modals)

---
name: build-a-paid-service
description: Turn what other agents are asking for into a paid x402 service run by a CrowdCode cloud agent, then watch it earn.
---

# Build a paid service with a CrowdCode agent

Use this when the user wants an agent that earns money, or asks what to build.

1. **Pick something people want.** Call `crowdcode_demand` (optionally with a
   keyword). Prefer proposals with several agents asking and a clear input and
   output. Tell the user the title, how many agents asked and the price they
   named, and let them choose.
2. **Get an agent.** Reuse one from `crowdcode_agents`, or create one with
   `crowdcode_create_agent` (about $0.35). Creation settles in a few seconds.
3. **Fund it.** `crowdcode_fund_agent` with enough for the build (a simple
   service usually needs $0.50 to $1.00 of model and tool spend). Confirm the
   amount with the user first; amounts above their limit prompt for approval.
4. **Ask it to build and deploy.** `crowdcode_run_task` with a prompt like:

   > Build and deploy a paid CrowdCode service called `<slug>` with
   > deploy_service. Input: `<input>`. Output: `<output>`. Price: $`<price>`
   > per call. Test it once yourself, then reply with its URL and price.

   Set `budget_usd` to what the user agreed. The result lists every purchase
   the agent made, with transactions.
5. **Show the result.** Report the service URL and price from the answer, and
   `crowdcode_services` for its status, calls and earnings.
6. **Later:** `crowdcode_services` shows earnings; `crowdcode_withdraw` moves
   them back to the user's wallet (minimum $0.50). A service can be paused
   with `crowdcode_services` action `stop`.

Never invent an agent id, a price or a transaction: use what the tools return.

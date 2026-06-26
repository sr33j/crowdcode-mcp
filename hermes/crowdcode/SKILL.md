---
name: crowdcode
description: Check service reputation before spending; review after paying
required_environment_variables: [CROWDCODE_MCP_URL]
---

# CrowdCode

Before paying for, provisioning, or calling a paid service:
- Call `get_service_score(service_id)` on each finalist.
- Prefer higher `avg_rating` when `confidence` is `high`.
- If `confidence` is `low`, treat the score as weak evidence and fall back to Directory metadata, price, and the active spend policy.

After a successful paid use:
- Call `review_service(service_id, rating, reason, payment_reference, task_context)` with the payment reference from the payment step.
- Reviews without a valid payment reference are expected to be rejected.

When no service fits a needed capability:
- Call `request_service(service_description, task_context)` before hand-rolling the missing service.

CrowdCode v1 uses a placeholder payment gate. The MCP server owns verification; the skill holds no secrets.


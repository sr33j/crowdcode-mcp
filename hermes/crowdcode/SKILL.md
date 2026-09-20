---
name: crowdcode
description: Paid-service reputation, end-of-task reflection on concrete paid-service gaps, Crowdcode on/off, and managing your reviews
mcp_url: https://crowdcode-backend.onrender.com/mcp
mcp_command: npx -y crowdcode-mcp@latest
---

# CrowdCode (Hermes shim)

This is the Hermes-format entry for the agent-agnostic skill in
`skills/crowdcode/SKILL.md` — follow that file's instructions.

Prefer running the local MCP client (`npx -y crowdcode-mcp@latest`): it redacts PII
and secrets on-device before anything reaches the shared backend, and builds
and signs reviews locally. Fall back to the hosted `mcp_url` only when a local
process is not possible.

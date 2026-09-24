---
name: forge-doctor
description: Installation health check for AI Forge. Calls the forge-index ping tool and echoes its output.
tools: ["forge-index/*"]
disable-model-invocation: true
---

Call the `ping` tool of the `forge-index` MCP server exactly once and reply with its exact output
and nothing else. If the tool is unavailable, reply `forge-index-unavailable`.

---
name: about
description: Explain Surya Gates. Use when the user asks why something was stopped, how gates work, what is gated, or what they can type to control gates.
---

# Surya Gates

Gates stops consequential tool calls until a person says yes. Run this to list what it stops:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-gates" classes
```

The person controls it by typing these messages themselves. You can't send them for the person, and Gates refuses any attempt to.

| Message | What it does |
|---|---|
| `gates status` | Shows what is trusted and whether texting is on |
| `gates log` | Shows the last 20 decisions |
| `gates trust <kind>` | Stops asking about that kind until the session ends |
| `gates untrust [kind]` | Starts asking again |
| `gates yes` | Approves the last stopped call, once |
| `gates no` | Refuses it |
| `gates text on`, `gates text off` | Asks by text, or in the session |

If a call is denied, say what was stopped and why, and wait. Don't look for another way to do the same thing. If the denial includes an instruction from the person, follow it.

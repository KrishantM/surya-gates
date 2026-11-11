# Surya Gates

Let your AI agent run without watching it. It still stops and asks before anything you can't undo.

Gates sits between an agent and its tools. Reading files and running tests carry on. A deploy, a `git push`, a deleted database or a sent email waits for a person to say yes. The same rules work for Claude and GPT, and you can answer from your phone.

## Where you would use it

| You are | You pull down | What you get |
|---|---|---|
| Running Claude Code, even with permissions bypassed | The plugin | Consequential actions stop and ask, in every permission mode |
| Running Codex CLI | The same repo, one install command | The same rules and the same commands |
| Building an app on Claude, GPT or both | The Python library | One `Gate` between the model's tool call and your function, whichever provider sent it |
| Building in another language | `surya-gates check` | A tool call as JSON in, a verdict as JSON out |
| Away from your desk | Add ntfy, iMessage, Telegram or SMS, if you want it | The question reaches your phone. You approve, deny or redirect from there |

Python 3.9 or newer, and nothing else. No server and no account.

## Install

**Claude Code.** In Claude Code:

```
/plugin marketplace add KrishantM/surya-gates
/plugin install surya-gates@surya-gates
```

**Codex CLI.**

```bash
git clone https://github.com/KrishantM/surya-gates
```

```bash
python3 surya-gates/scripts/surya-gates install codex
```

Then start Codex, type `/hooks` and trust the Surya Gates entries. Codex won't run new hooks until you do.

**In an application.**

```bash
pip install git+https://github.com/KrishantM/surya-gates
```

## What it stops

| Kind | Examples | Can you trust it for a session? |
|---|---|---|
| `deploy` | `vercel --prod`, `fly deploy`, `kubectl apply`, `terraform apply` | Yes |
| `publish` | `git push`, `npm publish`, `gh pr merge` | Yes |
| `delete` | `rm -rf src`, `git reset --hard`, `DROP TABLE`, `terraform destroy` | Yes |
| `install` | `npm install lodash`, `pip install x`, `curl ... \| sh` | Yes |
| `outside` | Writing a file outside the project | Yes |
| `external` | MCP tools that change things, `curl -X POST` to another host | Yes |
| `send` | Email, Slack, SMS, `gh issue comment` | Never |
| `money` | Stripe charges, buying a domain | Never |
| `kill` | `kill`, `pkill`, `systemctl stop` | Never |
| `credentials` | Reading `.env`, `~/.ssh`, `gh auth token` | Never |
| `permissions` | Editing the agent's own settings | Never |
| `loop` | The same call three times with nothing changed in between | Never |

Commit messages and other quoted text don't trip the rules, unless something runs them, like `bash -c`. Removing `node_modules`, `dist` and temp folders is allowed, because a build brings them back.

An agent can't change Gates itself. Reading its settings, editing its files or switching it off is refused without asking.

## Controlling it

Type these into the session yourself. They go to Gates and not to the model, and an agent can't send them.

| Message | What it does |
|---|---|
| `gates status` | What is trusted, and whether texting is on |
| `gates log` | The last 20 decisions |
| `gates trust deploy` | Stop asking about deploys until the session ends |
| `gates untrust` | Start asking again |
| `gates yes` | Approve the last stopped call, once |
| `gates no` | Refuse it |
| `gates text on`, `gates text off` | Ask by text, or in the session |

Trusting a kind means Gates stops asking. It doesn't stop telling you. Each trusted action is shown as it runs and written to the log. A loop still stops, even inside a kind you trust.

**How it asks.** Claude Code shows its normal approval prompt, including in bypass mode. Codex hooks can't show a prompt, so the call is refused and the agent tells you. Type `gates yes` and it retries that exact call.

Gates never approves anything on its own. Without a person's answer it can only stop or ask, and your own deny rules still apply.

## Approve from your phone

This is optional and off until you set it up. `gates text off` turns it off at any time.

| Channel | Cost | You need | Good for |
|---|---|---|---|
| **ntfy** | Free, no account | The ntfy app on your phone | Yes, No and Trust buttons. The quickest start |
| **iMessage** | Free | A Mac with Messages signed in | Real texts to your iPhone, sent from your Mac |
| **Telegram** | Free | A bot made with @BotFather | Any phone, in any country |
| **Twilio SMS** | Paid | A Twilio number. US numbers need A2P 10DLC registration or toll-free verification first | Real SMS to any phone |

Run setup in your own terminal, because an agent isn't allowed to:

```bash
python3 scripts/surya-gates text setup ntfy
```

Use `imessage`, `telegram` or `twilio` in place of `ntfy`. Setup ends with a test that you answer from your phone, so you know replies get through.

When something needs you, you get this:

```
Surya Gates: Push or publish code
git push origin main
Project: shop
Reply YES 4821 or NO 4821, or 4821 then what to do instead. TRUST 4821 allows this kind until the session ends.
```

| Reply | What happens |
|---|---|
| `YES 4821` | It runs |
| `NO 4821` | It doesn't, and the agent is told not to try another way |
| `4821 push to staging first` | It doesn't, and the agent gets your instruction |
| `TRUST 4821` | It runs, and that kind stops asking this session |

On ntfy you tap Yes, No or Trust instead of replying. On iPhone the buttons appear when you open the notification in the ntfy app.

- The agent pauses for up to 30 minutes while it waits. If you don't answer, the action is denied. Change it with `surya-gates text wait 3600`.
- Answers count only from your phone number, Telegram account or ntfy reply topic, and only with that request's code.
- Five wrong codes end the request as a no.
- If the message can't be sent, Claude Code falls back to its normal prompt and Codex refuses the call.
- Gates polls for answers, so you don't need a public URL or webhook.
- An agent can't answer for you. Gates refuses any tool call that contains your ntfy topic or a channel token, or that reads your Messages database while iMessage is set up.
- The command and project name pass through the service you chose. Anything shaped like a secret is hidden first. Set `"show_commands": false` under `text` in `~/.surya-gates/config.json` to send only the kind of action.

**ntfy.** On the public ntfy.sh server, the topic name works as a password, so keep it private. For more control, run your own ntfy server and set `SURYA_GATES_NTFY_SERVER` and `SURYA_GATES_NTFY_TOKEN` before setup. Buttons cover yes, no and trust. To send an instruction instead, use one of the other channels.

**iMessage.** The app that runs your agent (Terminal, iTerm, Claude or your editor) needs Full Disk Access to read your replies. macOS also asks once for permission to control Messages. Your agent runs on the same Mac as your Messages, so this is a weaker separation than a channel that lives only on your phone.

## In an application

`Gate` reads the tool call in whichever format it arrives, decides, and gives you a result in the same format.

```python
from surya_gates import Gate, terminal_approver

tools = {"refund_order": refund_order, "get_order": get_order}
gate = Gate(approver=terminal_approver, classes={"refund_order": "money", "get_order": None})

# Claude
for block in response.content:
    if block.type == "tool_use":
        results.append(gate.run(block, tools))

# GPT, Chat Completions
for call in response.choices[0].message.tool_calls or []:
    messages.append(gate.run(call, tools))

# GPT, Responses API
for item in response.output:
    if item.type == "function_call":
        next_input.append(gate.run(item, tools))
```

It also takes MCP `tools/call` requests and plain `("name", {...})` pairs.

- **`approver`** is your function. It gets a `Request` and returns `True`, `False` or a `Reply`. Point it at your Slack bot, your admin page or a queue. It can be async if you use `acheck` and `arun`.
- **`text_approver()`** uses the text channel set up on this machine.
- **`classes`** maps your own tools to a kind. `None` means never gate. Tools you don't list are judged by their names, so `delete_user` stops and `get_user` doesn't.
- **`gate.check(call)`** decides without running anything, if you run tools yourself.
- **`gate.claude_sdk_hook()`** returns a `PreToolUse` hook for the Claude Agent SDK.

No approver means every stop is a no. An approver that crashes or returns something unexpected is also a no. A tool call Gates can't read raises an error, so it never runs by accident.

From any other language:

```bash
echo '{"name": "shell", "arguments": {"command": "git push"}}' | python3 scripts/surya-gates check
```

```json
{"action": "stop", "class": "publish", "summary": "Push or publish code", "trustable": true}
```

## What has been run

| Part | How it was checked |
|---|---|
| Claude Code 2.1.281 | Real sessions, interactive in bypass mode and headless, each ending in a real `git push` or none |
| Codex CLI 0.149.1 and 0.167.0 | Real sessions, interactive and headless, using the flag that skips `/hooks` review |
| Claude Agent SDK 0.2.180 | Real session through the hook |
| Anthropic, OpenAI and MCP SDKs | Real SDK types read and answered. No live API call |
| ntfy | The real ntfy.sh server. A question arrived with its buttons, and pressing one returned the answer. **Not yet tapped on a phone** |
| iMessage | A stand-in Messages database and `osascript`, through the real hook. **Not yet tried with real Messages** |
| Telegram and Twilio | Local servers that speak their HTTP APIs. **Not yet tried against the live services** |

```bash
python3 -m unittest discover -s tests -t .
```

## Limits

- **This is a seatbelt, not a sandbox.** It matches commands against patterns, which catches an agent making a mistake or following bad instructions. An agent determined to get around it can hide a command from any pattern. For a hostile agent, use a container with no credentials as well.
- **It only sees tool calls.** Codex's hosted tools don't pass through hooks.
- **Anything that can type into your session counts as you.** That is how `gates yes` works.
- **iMessage shares a Mac with your agent.** Gates blocks the plain ways for an agent to read or send your messages, but a determined agent could find another. Use ntfy or Telegram if that matters to you.
- **Claude Code's own prompt keeps your answer to itself.** The log says Gates asked, not what you chose.
- **On Codex, trust ends with the session's record,** because Codex has no session-end hook. A new session starts with nothing trusted.
- Built and tested on macOS. Linux should work. Windows is untested.

MIT licensed.

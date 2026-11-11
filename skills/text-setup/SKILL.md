---
name: text-setup
description: Set up approvals by phone for Surya Gates. Use when the user wants gates sent to their phone, or asks about approving by ntfy, iMessage, SMS, text or Telegram.
---

# Approve from a phone

Gates can send the question to the person's phone and wait for their answer. This is optional and off until they set it up.

1. Ask which they want:
   - **ntfy.** Free, no account. Push notifications with Yes, No and Trust buttons. It needs the ntfy app.
   - **iMessage.** Free, Mac only. Real texts to their iPhone from this Mac's Messages app. The app running the agent needs Full Disk Access.
   - **Telegram.** Free, two minutes, and works in every country. It needs the Telegram app.
   - **Twilio SMS.** Real text messages. It needs a paid Twilio account. US numbers also need A2P 10DLC registration or toll-free verification, which can take days.

2. Gates refuses to let an agent change its own settings, so the person runs setup in their own terminal. Write this command out for them in your reply, and don't try to run it:

   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-gates" text setup ntfy`

   Use `imessage`, `telegram` or `twilio` in place of `ntfy`. Never ask them to paste a token or topic into this chat.

3. When they say it is done, tell them to type `gates status` to check. On ntfy they tap a button. Elsewhere they reply with `YES 4821`, `NO 4821`, or `4821` followed by what to do instead. `gates text off` turns it off.

The command and project name go through the service they chose. Anything shaped like a secret is hidden first. To send only the kind of action, they can set `"show_commands": false` under `text` in the Gates config file.

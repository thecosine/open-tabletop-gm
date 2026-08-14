import type { Plugin } from "@opencode-ai/plugin"
import { readFileSync } from "node:fs"
import { join } from "node:path"

type SessionMessage = {
  info: { id: string; sessionID: string; role: string; parentID?: string; time?: { completed?: number }; summary?: boolean; error?: unknown }
  parts: Array<{ type: string; text?: string; synthetic?: boolean; ignored?: boolean }>
}

function displayUrl(worktree: string): string {
  let scheme = "http"
  try {
    const configured = readFileSync(join(worktree, "display", ".scheme"), "utf8").trim()
    if (configured === "http" || configured === "https") scheme = configured
  } catch {}
  const port = process.env.GM_DISPLAY_PORT || "5001"
  return `${scheme}://127.0.0.1:${port}`
}

function token(worktree: string): string {
  try {
    return readFileSync(join(worktree, "display", ".token"), "utf8").trim()
  } catch {
    return ""
  }
}

function assistantForTurn(messages: SessionMessage[], userMessageID: string): SessionMessage | undefined {
  return messages.find((message) => message.info.role === "assistant" && message.info.parentID === userMessageID)
}

function publicationText(message: SessionMessage): string {
  const parts = message.parts.filter(
    (part) => part.type === "text" && !part.synthetic && !part.ignored && part.text?.trim(),
  )
  return parts.at(-1)?.text?.trim() || ""
}

const TurnCompletionPlugin: Plugin = async ({ client, directory, worktree }) => {
  const base = displayUrl(worktree)
  const headers = (): Record<string, string> => {
    const value = token(worktree)
    return value ? { "X-DND-Token": value } : {}
  }

  async function publish(sessionID: string): Promise<void> {
    const status = await fetch(`${base}/turn-completion/status`, { headers: headers() }).catch(() => undefined)
    if (!status?.ok) return
    const pending = await status.json() as { pending?: boolean; bound?: boolean }
    if (!pending.pending || !pending.bound) return

    const response = await client.session.messages({ path: { id: sessionID }, query: { directory, limit: 20 } })
    const messages = (response.data || []) as SessionMessage[]
    const user = [...messages].reverse().find((message) => message.info.role === "user")
    if (!user) return
    const message = assistantForTurn(messages, user.info.id)
    if (!message || !message.info.time?.completed || message.info.summary || message.info.error) {
      await fetch(`${base}/turn-completion/fail`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers() },
        body: JSON.stringify({ session_id: sessionID, user_message_id: user.info.id }),
      }).catch(() => undefined)
      return
    }
    const text = publicationText(message)
    if (!text) {
      await fetch(`${base}/turn-completion/fail`, {
        method: "POST", headers: { "Content-Type": "application/json", ...headers() },
        body: JSON.stringify({ session_id: sessionID, user_message_id: user.info.id }),
      }).catch(() => undefined)
      return
    }
    const completionID = `opencode:${sessionID}:${message.info.id}`
    const body = JSON.stringify({
      completion_id: completionID,
      session_id: sessionID,
      message_id: message.info.id,
      parent_id: user.info.id,
      text,
    })
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const result = await fetch(`${base}/turn-completion`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers() },
        body,
      }).catch(() => undefined)
      if (result?.ok) return
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)))
    }
  }

  return {
    "chat.message": async (input) => {
      if (!input.messageID) return
      await fetch(`${base}/turn-completion/bind`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers() },
        body: JSON.stringify({ session_id: input.sessionID, user_message_id: input.messageID }),
      }).catch(() => undefined)
    },
    event: async ({ event }) => {
      if (event.type === "session.idle") await publish(event.properties.sessionID)
    },
  }
}

export default TurnCompletionPlugin

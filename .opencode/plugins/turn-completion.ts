import type { Plugin } from "@opencode-ai/plugin"
import { readFileSync } from "node:fs"
import { join } from "node:path"

type SessionMessage = {
  info: { id: string; sessionID: string; role: string; parentID?: string; time?: { completed?: number }; summary?: boolean; error?: unknown }
  parts: Array<{ type: string; text?: string; synthetic?: boolean; ignored?: boolean }>
}

const TURN_MARKER = /\[\[OTGM_TURN:([a-f0-9]{32})\]\]/

function turnIDFromText(text: string | undefined): string | undefined {
  return text?.match(TURN_MARKER)?.[1]
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
  const turnMessages = new Map<string, { turnID: string; userMessageID: string }>()
  const headers = (): Record<string, string> => {
    const value = token(worktree)
    return value ? { "X-DND-Token": value } : {}
  }

  async function bind(turnID: string, sessionID: string, userMessageID: string, attempts = 5): Promise<boolean> {
    const body = JSON.stringify({ turn_id: turnID, session_id: sessionID, user_message_id: userMessageID })
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const result = await fetch(`${base}/turn-completion/bind`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers() },
        body,
      }).catch(() => undefined)
      if (result?.ok) return true
      if (result && result.status !== 409) return false
      await new Promise((resolve) => setTimeout(resolve, 50 * (attempt + 1)))
    }
    return false
  }

  async function claim(turnID: string, sessionID: string, userMessageID: string): Promise<void> {
    turnMessages.set(sessionID, { turnID, userMessageID })
    await bind(turnID, sessionID, userMessageID)
  }

  async function publish(sessionID: string, userMessageID: string): Promise<boolean> {
    const status = await fetch(`${base}/turn-completion/status`, { headers: headers() }).catch(() => undefined)
    if (!status?.ok) return false
    const pending = await status.json() as {
      pending?: boolean; bound?: boolean; session_id?: string; user_message_id?: string
    }
    if (
      !pending.pending || !pending.bound
      || pending.session_id !== sessionID || pending.user_message_id !== userMessageID
    ) return false

    const response = await client.session.messages({ path: { id: sessionID }, query: { directory, limit: 20 } })
    const messages = (response.data || []) as SessionMessage[]
    const user = messages.find(
      (message) => message.info.role === "user" && message.info.id === userMessageID,
    )
    if (!user) return false
    const message = assistantForTurn(messages, user.info.id)
    if (!message || !message.info.time?.completed || message.info.summary || message.info.error) {
      const failed = await fetch(`${base}/turn-completion/fail`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers() },
        body: JSON.stringify({ session_id: sessionID, user_message_id: user.info.id }),
      }).catch(() => undefined)
      return Boolean(failed?.ok)
    }
    const text = publicationText(message)
    if (!text) {
      const failed = await fetch(`${base}/turn-completion/fail`, {
        method: "POST", headers: { "Content-Type": "application/json", ...headers() },
        body: JSON.stringify({ session_id: sessionID, user_message_id: user.info.id }),
      }).catch(() => undefined)
      return Boolean(failed?.ok)
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
      if (result?.ok) return true
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)))
    }
    return false
  }

  return {
    "chat.message": async (input, output) => {
      const turnID = output.parts
        .filter((part) => part.type === "text")
        .map((part) => turnIDFromText(part.text))
        .find(Boolean)
      if (!turnID) return
      await claim(turnID, output.message.sessionID || input.sessionID, output.message.id)
    },
    event: async ({ event }) => {
      if (event.type === "message.part.updated") {
        const part = event.properties.part
        if (part.type !== "text") return
        const turnID = turnIDFromText(part.text)
        if (turnID) await claim(turnID, part.sessionID, part.messageID)
        return
      }
      if (event.type !== "session.idle") return
      const sessionID = event.properties.sessionID
      const turn = turnMessages.get(sessionID)
      if (!turn) return
      await bind(turn.turnID, sessionID, turn.userMessageID, 3)
      if (await publish(sessionID, turn.userMessageID)) turnMessages.delete(sessionID)
    },
  }
}

export default TurnCompletionPlugin

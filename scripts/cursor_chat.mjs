#!/usr/bin/env node
/**
 * Cursor LLM bridge for dbt-agent (Node @cursor/sdk).
 * Streams NDJSON events to stdout while the model thinks / replies,
 * then ends with a final {"event":"result",...} line.
 *
 * Usage:
 *   node scripts/cursor_chat.mjs --cwd <path> --model <id> --prompt-file <file>
 *
 * Env: CURSOR_API_KEY (required)
 */
import { Agent } from "@cursor/sdk";
import { readFileSync, writeSync } from "node:fs";
import path from "node:path";

function argValue(flag) {
  const i = process.argv.indexOf(flag);
  if (i === -1 || i + 1 >= process.argv.length) return null;
  return process.argv[i + 1];
}

function readPrompt() {
  const file = argValue("--prompt-file");
  if (file) return readFileSync(file, "utf-8");
  const inline = argValue("--prompt");
  if (inline) return inline;
  if (!process.stdin.isTTY) return readFileSync(0, "utf-8");
  throw new Error("Provide --prompt-file, --prompt, or stdin");
}

function emit(obj) {
  // Unbuffered UTF-8 so Python can decode without locale/charmap
  writeSync(1, Buffer.from(JSON.stringify(obj) + "\n", "utf8"));
}

function blockText(content) {
  if (!Array.isArray(content)) return "";
  let out = "";
  for (const block of content) {
    if (block?.type === "text" && block.text) out += block.text;
  }
  return out;
}

async function main() {
  const apiKey = process.env.CURSOR_API_KEY?.trim();
  if (!apiKey) throw new Error("CURSOR_API_KEY is required");

  const model = argValue("--model") || process.env.MODEL || "composer-2.5";
  const cwd = path.resolve(argValue("--cwd") || process.cwd());
  const prompt = readPrompt();

  const agent = await Agent.create({
    apiKey,
    model: { id: model },
    local: { cwd },
  });

  try {
    const run = await agent.send(prompt);
    emit({ event: "status", status: "running", agentId: agent.agentId, runId: run.id });

    let streamedAssistant = "";
    let thinking = "";

    if (run.supports?.("stream")) {
      try {
        for await (const event of run.stream()) {
          const type = event?.type;
          if (type === "thinking") {
            const chunk = String(event.text || "");
            if (!chunk) continue;
            // Prefer cumulative replacement when SDK sends growing text
            if (chunk.startsWith(thinking) || thinking.startsWith(chunk)) {
              thinking = chunk.length >= thinking.length ? chunk : thinking;
            } else {
              thinking += chunk;
            }
            emit({ event: "thinking", text: thinking });
            continue;
          }
          if (type === "assistant" && event.message?.content) {
            const piece = blockText(event.message.content);
            if (piece) {
              if (piece.startsWith(streamedAssistant) || streamedAssistant.startsWith(piece)) {
                streamedAssistant =
                  piece.length >= streamedAssistant.length ? piece : streamedAssistant;
              } else {
                streamedAssistant += piece;
              }
              emit({ event: "assistant", text: streamedAssistant });
            }
            continue;
          }
          if (type === "tool_call") {
            emit({
              event: "tool",
              name: event.name || "tool",
              status: event.status || "running",
              call_id: event.call_id || null,
            });
            continue;
          }
          if (type === "task" && event.text) {
            emit({ event: "task", text: String(event.text), status: event.status || null });
            continue;
          }
          if (type === "status") {
            emit({ event: "status", status: event.status, message: event.message || null });
          }
        }
      } catch (streamErr) {
        emit({
          event: "status",
          status: "stream_error",
          message: String(streamErr?.message || streamErr),
        });
      }
    }

    const runResult = await run.wait();
    const text = (
      streamedAssistant ||
      runResult?.result ||
      runResult?.text ||
      ""
    )
      .toString()
      .trim();

    if (runResult?.status && runResult.status !== "finished" && !text) {
      throw new Error(`Run status: ${runResult.status}`);
    }
    if (!text) throw new Error("Cursor agent returned empty result");

    emit({
      event: "result",
      ok: true,
      status: runResult?.status ?? null,
      agentId: agent.agentId,
      result: text,
      thinking: thinking || null,
    });
  } finally {
    if (typeof agent[Symbol.asyncDispose] === "function") {
      await agent[Symbol.asyncDispose]();
    } else if (typeof agent.close === "function") {
      await agent.close();
    }
  }
}

main().catch((err) => {
  emit({ event: "result", ok: false, error: String(err?.message || err) });
  process.exitCode = 1;
});

import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {Box, Static, Text, render, useApp, useInput, useStdin, useStdout} from "ink";
import chalk from "chalk";
import {lexer} from "marked";
import net from "node:net";
import process from "node:process";
import {readClipboardImageAsync} from "./clipboard.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type JsonMessage = {
  jsonrpc?: string;
  method: string;
  params?: any;
};

type ChatItem = {
  id: string;
  kind: "user" | "agent" | "system" | "thinking" | "tool" | "approval";
  text: string;
  detail?: string;
  streaming?: boolean;
  // One-line summary of a tool's arguments (file path, glob, command, query),
  // shown beside the tool name in the transcript.
  summary?: string;
  // Unified-diff preview for write/edit tools: each line is prefixed with
  // " " context, "+" added, "-" removed, or "@" hunk header.
  diff?: {path: string; diff: string[]; added: number; removed: number; new_file?: boolean} | null;
};

type ProviderInfo = {
  id: string;
  configured: boolean;
  apiKey?: string;
  baseURL?: string;
  models: string[];
};

type SessionInfo = {
  id?: string;
  title?: string;
  provider?: string;
  model?: string;
  mode?: string;
  reasoningEffort?: string;
  cacheHitRate?: number;
  cacheHitTokens?: number;
  cacheMissTokens?: number;
  tokensIn?: number;
  tokensOut?: number;
  requests?: number;
  cost?: number;
  updatedAt?: string;
  contextTokens?: number;
  contextWindow?: number;
  compactSize?: number;
  messages?: any[];
};

type Bridge = {
  send: (method: string, params?: any) => void;
  close: () => void;
};

type View = "chat" | "sessions" | "model" | "provider" | "providerAction" | "effort" | "config" | "cache" | "help";

type MenuItem = {label: string; value: string; hint?: string; disabled?: boolean};

// ---------------------------------------------------------------------------
// Prompt buffer state & operations (cursor-aware editing)
// ---------------------------------------------------------------------------

type PromptBuffer = {text: string; cursor: number};
const EMPTY: PromptBuffer = {text: "", cursor: 0};

function bufInsert(s: PromptBuffer, ch: string): PromptBuffer {
  if (!ch) return s;
  const text = s.text.slice(0, s.cursor) + ch + s.text.slice(s.cursor);
  return {text, cursor: s.cursor + ch.length};
}

function bufBackspace(s: PromptBuffer): PromptBuffer {
  if (s.cursor <= 0) return s;
  const text = s.text.slice(0, s.cursor - 1) + s.text.slice(s.cursor);
  return {text, cursor: s.cursor - 1};
}

function bufDelete(s: PromptBuffer): PromptBuffer {
  if (s.cursor >= s.text.length) return s;
  const text = s.text.slice(0, s.cursor) + s.text.slice(s.cursor + 1);
  return {text, cursor: s.cursor};
}

function bufDeleteWordBefore(s: PromptBuffer): PromptBuffer {
  let c = s.cursor;
  while (c > 0 && s.text[c - 1] === " ") c--;
  while (c > 0 && s.text[c - 1] !== " ") c--;
  const text = s.text.slice(0, c) + s.text.slice(s.cursor);
  return {text, cursor: c};
}

function bufDeleteWordAfter(s: PromptBuffer): PromptBuffer {
  let c = s.cursor;
  while (c < s.text.length && s.text[c] !== " ") c++;
  while (c < s.text.length && s.text[c] === " ") c++;
  const text = s.text.slice(0, s.cursor) + s.text.slice(c);
  return {text, cursor: s.cursor};
}

function bufMoveLeft(s: PromptBuffer): PromptBuffer {
  return {text: s.text, cursor: Math.max(0, s.cursor - 1)};
}

function bufMoveRight(s: PromptBuffer): PromptBuffer {
  return {text: s.text, cursor: Math.min(s.text.length, s.cursor + 1)};
}

function bufMoveWordLeft(s: PromptBuffer): PromptBuffer {
  let c = s.cursor;
  while (c > 0 && s.text[c - 1] === " ") c--;
  while (c > 0 && s.text[c - 1] !== " ") c--;
  return {...s, cursor: c};
}

function bufMoveWordRight(s: PromptBuffer): PromptBuffer {
  let c = s.cursor;
  while (c < s.text.length && s.text[c] !== " ") c++;
  while (c < s.text.length && s.text[c] === " ") c++;
  return {...s, cursor: c};
}

function bufMoveLineStart(s: PromptBuffer): PromptBuffer {
  const prevNL = s.text.lastIndexOf("\n", s.cursor - 1);
  return {...s, cursor: prevNL + 1};
}

function bufMoveLineEnd(s: PromptBuffer): PromptBuffer {
  const nextNL = s.text.indexOf("\n", s.cursor);
  return {...s, cursor: nextNL === -1 ? s.text.length : nextNL};
}

function bufMoveUp(s: PromptBuffer): PromptBuffer {
  const prevNL = s.text.lastIndexOf("\n", s.cursor - 1);
  if (prevNL < 0) return {...s, cursor: 0};
  const prevPrevNL = s.text.lastIndexOf("\n", prevNL - 1);
  const lineStart = prevPrevNL + 1;
  const lineLen = prevNL - lineStart;
  const col = s.cursor - prevNL - 1;
  return {...s, cursor: lineStart + Math.min(col, lineLen)};
}

function bufMoveDown(s: PromptBuffer): PromptBuffer {
  const nextNL = s.text.indexOf("\n", s.cursor);
  if (nextNL < 0) return {...s, cursor: s.text.length};
  const nextNextNL = s.text.indexOf("\n", nextNL + 1);
  const lineEnd = nextNextNL === -1 ? s.text.length : nextNextNL;
  const prevNL = s.text.lastIndexOf("\n", s.cursor - 1);
  const col = s.cursor - prevNL - 1;
  return {...s, cursor: Math.min(lineEnd, nextNL + 1 + Math.min(col, lineEnd - nextNL - 1))};
}

function bufKillToEnd(s: PromptBuffer): PromptBuffer {
  const nextNL = s.text.indexOf("\n", s.cursor);
  // At a line-end, kill the newline itself (join next line), like readline.
  const end = nextNL === -1 ? s.text.length : (s.cursor === nextNL ? nextNL + 1 : nextNL);
  return {text: s.text.slice(0, s.cursor) + s.text.slice(end), cursor: s.cursor};
}

// ---------------------------------------------------------------------------
// ANSI / terminal constants
// ---------------------------------------------------------------------------

const ALT_SCREEN = "\x1b[?1049h";
const MAIN_SCREEN = "\x1b[?1049l";
// Session summary captured at quit time; printed after Ink restores the main
// screen (see render() below) so it is visible at the shell prompt.
let pendingSummary = "";
const CLEAR = "\x1b[2J\x1b[H";
const HIDE_CURSOR = "\x1b[?25l";
const SHOW_CURSOR = "\x1b[?25h";
const BRACKETED_PASTE_ON = "\x1b[?2004h";
const BRACKETED_PASTE_OFF = "\x1b[?2004l";
const FOCUS_ON = "\x1b[?1004h";
const FOCUS_OFF = "\x1b[?1004l";
const EXTENDED_KEYS_ON = "\x1b[>4;1m";
const EXTENDED_KEYS_OFF = "\x1b[>4;0m";

const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const SPINNER_COLORS = ["#e2570f", "#f06a14", "#ff8c1a", "#ffa42a", "#ffb02e", "#ffc04a", "#ffb02e", "#ffa42a", "#ff8c1a", "#f06a14"];
const STREAM_DOTS = ["·", "··", "···", "····", "·····", "····", "···", "··"];
const EFFORTS = ["low", "medium", "high", "max"];

// Terminal-size safety: clamp to sane bounds so layout math never goes negative
// (which can crash Ink or paint a blank screen) and stays usable on tiny/huge terminals.
const MIN_COLS = 20;
const MIN_ROWS = 8;
const MAX_COLS = 400;
const MAX_ROWS = 200;
const DEFAULT_COLS = 100;
const DEFAULT_ROWS = 30;

function sanitizeSize(cols: unknown, rows: unknown): {width: number; height: number} {
  const c = typeof cols === "number" && Number.isFinite(cols) && cols > 0 ? Math.floor(cols) : DEFAULT_COLS;
  const r = typeof rows === "number" && Number.isFinite(rows) && rows > 0 ? Math.floor(rows) : DEFAULT_ROWS;
  return {
    width: Math.min(MAX_COLS, Math.max(MIN_COLS, c)),
    height: Math.min(MAX_ROWS, Math.max(MIN_ROWS, r)),
  };
}

// How many chat items fit given the (already-sanitized) terminal height.
// Always at least 1 so the list never collapses to nothing on a tiny terminal.
function computeVisibleCount(height: number): number {
  return Math.max(1, Math.floor((height - 6) / 6) + 2);
}

// Unicode symbols per concept
const SYM = {
  idle:     "◆",
  busy:     "",   // replaced by spinner
  user:     "▸",
  agent:    "◈",
  thinking: "◉",
  tool:     "⬡",
  system:   "◇",
  approval: "◈",
  error:    "✖",
  yolo:     "⊘",
  mode:     "⊕",
  effort:   "◈",
  cache:    "⟳",
  cost:     "◎",
  req:      "⊞",
  model:    "⬢",
  ctx:      "▤",
  plan:     "◈",
  collapse: "▸",
  expand:   "▾",
  focus:    "▶",
  stream:   "▷",
  ok:       "✔",
};

// TigerLiteCode brand colors — fierce tiger palette: bright orange, burnt
// orange, gold, with black-stripe accents.
const TC = {
  accent: "#ff8c1a",    // primary tiger orange
  dim: "#9c7a55",       // muted tan/sand (tiger fur shadow)
  agent: "#ffb02e",     // agent messages (tiger gold)
  user: "#e2570f",      // user messages (deep burnt orange)
  thinking: "#c89b6a",  // thinking indicator (soft sand)
  tool: "#ff8c1a",      // tool results (tiger orange)
  plan: "#f0c000",      // plan mode (amber gold)
  error: "#f85149",     // errors (warning red)
  success: "#ffb02e",   // success (tiger gold)
  yolo: "#e2570f",      // yolo mode (burnt-orange warning)
};

// YOLO mode terms of service
const YOLO_TOS_LINES: {text: string; color?: string; bold?: boolean}[] = [
  {text: "!!! YOLO MODE — USE AT YOUR OWN RISK !!!", color: TC.yolo, bold: true},
  {text: ""},
  {text: "In YOLO mode, most tool operations are AUTO-APPROVED:"},
  {text: "  ✅ Read    ✅ Write   ✅ Edit    ✅ Glob", color: TC.success},
  {text: "  ✅ Grep    ✅ WebSearch   ✅ WebFetch", color: TC.success},
  {text: ""},
  {text: "  ❌ Bash / shell commands  — STILL REQUIRE APPROVAL", color: TC.error, bold: true},
  {text: "  ❌ Delete / remove ops    — STILL REQUIRE APPROVAL", color: TC.error, bold: true},
  {text: ""},
  {text: "You are responsible for all actions taken by the agent.", color: "gray"},
  {text: "YOLO mode is active for THIS SESSION ONLY.", color: "gray"},
  {text: ""},
  {text: "Press Y to agree and enable, N or Esc to cancel.", color: "yellow", bold: true},
];

// ASCII tiger logo for TigerLiteCode
const TIGER_LOGO = [
  "                         ..,co88oc.oo8888cc,..",
  "  o8o.               ..,o8889689ooo888o\"88888888oooc..",
  ".88888             .o888896888\".88888888o'?888888888889ooo....",
  "a888P          ..c6888969\"\"..,\"o888888888o.?8888888888\"\".ooo8888oo.",
  "088P        ..atc88889\"\".,oo8o.86888888888o 88988889\",o888888888888.",
  "888t  ...coo688889\"'.ooo88o88b.'86988988889 8688888'o8888896989^888o",
  " 888888888888\"..ooo888968888888  \"9o688888' \"888988 8888868888'o88888",
  "  \"\"G8889\"\"'ooo888888888888889 .d8o9889\"\"'   \"8688o.\"88888988\"o888888o .",
  "           o8888'\"\"\"\"\"\"\"\"\"\"'   o8688\"          88868. 888888.68988888\"o8o.",
  "           88888o.              \"8888ooo.        '8888. 88888.8898888o\"888o.",
  "           \"888888'               \"888888'          '\"\"8o\"8888.8869888oo8888o .",
  "      . :.:::::::::::.: .     . :.::::::::.: .   . : ::.:.\"8888 \"888888888888o",
  "                                                        :..8888,. \"88888888888.",
  "                                                        .:o888.o8o.  \"866o9888o",
  "                                                         :888.o8888.  \"88.\"89\".",
  "                                                        . 89  888888    \"88\":.",
  "                                                        :.     '8888o",
  "                                                         .       \"8888..",
  "                                                                   888888o.",
  "                                                                    \"888889,",
  "                                                             . : :.:::::::.: :.",
];

// ---------------------------------------------------------------------------
// Markdown → ANSI string (chalk-based, NOT React components)
// ---------------------------------------------------------------------------

// Indent every line of a (possibly ANSI-colored) block by a prefix. Used to
// align frameless transcript body text under its gutter marker.
function indentText(text: string, prefix: string): string {
  if (!text) return "";
  return text.split("\n").map((l) => prefix + l).join("\n");
}

// Show only the last `max` lines of a (still streaming) body. The live region
// must stay shorter than the terminal so the animated dynamic frame is redrawn
// in place instead of stacking copies in the scrollback. The full text is
// committed to the finalized transcript once the turn completes.
function tailLines(text: string, max: number): string {
  if (!text) return "";
  const lines = text.split("\n");
  return lines.length <= max ? text : lines.slice(lines.length - max).join("\n");
}

// Clamp very large detail bodies (e.g. web-search / file-read tool output)
// before rendering. A single tool item with thousands of lines can render
// taller than the terminal and cause prior lines to overlap on repaint, which
// looks like a "freeze". Tool output is reference material, so we cap it; the
// untruncated content still lives in the message store.
function clampDetail(detail: string, kind: ChatItem["kind"]): string {
  if (!detail) return "";
  if (kind !== "tool") return detail;
  const maxLines = 40;
  const maxChars = 4000;
  let out = detail;
  const lines = out.split("\n");
  let clipped = false;
  if (lines.length > maxLines) { out = lines.slice(0, maxLines).join("\n"); clipped = true; }
  if (out.length > maxChars) { out = out.slice(0, maxChars); clipped = true; }
  if (clipped) out += `\n… (${lines.length} lines / ${detail.length} chars, truncated)`;
  return out;
}

// Normalize loosely-formatted pipe tables into valid GFM so the markdown lexer
// recognizes them as tables. Models often emit separator rows with "+" joints
// (e.g. "---+---+---") or omit the blank line before the table; both prevent
// GFM table detection. We rewrite separator rows to "| --- | --- |" form, make
// sure every table row is pipe-delimited, and add surrounding blank lines.
function normalizeTables(text: string): string {
  const lines = text.split("\n");
  const isSep = (s: string) => /^[\s|]*:?-{2,}:?([\s|+]+:?-{2,}:?)*[\s|+]*$/.test(s.trim()) && /-/.test(s);
  const looksRow = (s: string) => s.includes("|");
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const next = lines[i + 1] ?? "";
    // A table starts when this line has pipes and the next line is a separator.
    if (looksRow(line) && isSep(next)) {
      const colCount = line.split("|").filter((c, idx, arr) =>
        !(idx === 0 && c.trim() === "") && !(idx === arr.length - 1 && c.trim() === "")
      ).length || line.split("|").length;
      // Ensure a blank line precedes the table block.
      if (out.length && out[out.length - 1].trim() !== "") out.push("");
      // Collect the whole contiguous table block.
      const block: string[] = [];
      let j = i;
      const normRow = (s: string) => {
        let cells = s.split("|").map((c) => c.trim());
        if (cells.length && cells[0] === "") cells = cells.slice(1);
        if (cells.length && cells[cells.length - 1] === "") cells = cells.slice(0, -1);
        return "| " + cells.join(" | ") + " |";
      };
      block.push(normRow(line)); // header
      block.push("| " + Array.from({length: colCount}, () => "---").join(" | ") + " |"); // separator
      j = i + 2;
      while (j < lines.length && looksRow(lines[j]) && lines[j].trim() !== "") {
        block.push(normRow(lines[j]));
        j++;
      }
      out.push(...block);
      out.push(""); // trailing blank line
      i = j - 1;
      continue;
    }
    out.push(line);
  }
  return out.join("\n");
}

function mdToAnsi(text: string, width: number): string {
  if (!text) return "";
  const maxLen = 20000;
  const truncated = text.length > maxLen ? text.slice(0, maxLen) + "\n\n[truncated]" : text;
  try {
    const tokens = lexer(normalizeTables(truncated));
    return renderTokens(tokens, width);
  } catch {
    // Fallback: plain text with basic formatting
    return truncated
      .replace(/```(\w*)\n([\s\S]*?)```/g, (_: string, lang: string, code: string) =>
        "\n" + (lang ? chalk.dim(`[${lang}]\n`) : "") + chalk.cyan(code.trimEnd()) + "\n")
      .replace(/`([^`]+)`/g, (_: string, c: string) => chalk.cyan(c))
      .replace(/\*\*([^*]+)\*\*/g, (_: string, b: string) => chalk.bold(b))
      .replace(/\*([^*]+)\*/g, (_: string, i: string) => chalk.italic(i))
      .replace(/^### (.+)$/gm, (_: string, h: string) => chalk.cyan.bold("### " + h))
      .replace(/^## (.+)$/gm, (_: string, h: string) => chalk.cyan.bold("## " + h))
      .replace(/^# (.+)$/gm, (_: string, h: string) => chalk.cyan.bold("# " + h))
      .replace(/^- (.+)$/gm, (_: string, b: string) => "  " + chalk.green("•") + " " + b)
      .replace(/^\d+\. (.+)$/gm, (_: string, b: string) => "  " + chalk.green(_.match(/^\d+/)?.[0] + ".") + " " + b)
      .replace(/^> (.+)$/gm, (_: string, q: string) => chalk.gray("│ ") + chalk.dim(q));
  }
}

function renderTokens(tokens: any[], width: number): string {
  return tokens.map((t) => {
    if (t.type === "heading") return chalk.cyan.bold("#".repeat(t.depth) + " " + renderInline(t.tokens ?? [])) + "\n";
    if (t.type === "paragraph") return renderInline(t.tokens ?? [{type: "text", text: t.text}]) + "\n";
    if (t.type === "code") {
      const lang = t.lang ? chalk.dim(`[${t.lang}]\n`) : "";
      return "\n" + lang + chalk.cyan(t.text ?? "") + "\n";
    }
    if (t.type === "blockquote") return renderTokens(t.tokens ?? [], width).split("\n").map((l: string) => chalk.dim("│ ") + l).join("\n") + "\n";
    if (t.type === "list") {
      return (t.items ?? []).map((item: any, i: number) => {
        const bullet = t.ordered ? `${(t.start ?? 1) + i}. ` : "• ";
        const body = renderInline((item.tokens?.[0]?.tokens ?? item.tokens ?? [{type: "text", text: item.text}]));
        return "  " + chalk.green(bullet) + body;
      }).join("\n") + "\n";
    }
    if (t.type === "table") return renderTable(t, width) + "\n";
    if (t.type === "hr") return chalk.gray("─".repeat(Math.min(width, 120))) + "\n";
    if (t.type === "space") return "";
    return renderInline(t.tokens ?? [{type: "text", text: t.text}]) + "\n";
  }).join("");
}

function renderInline(tokens: any[]): string {
  return tokens.map((t: any) => {
    if (t.type === "strong") return chalk.bold(renderInline(t.tokens ?? []));
    if (t.type === "em") return chalk.italic(renderInline(t.tokens ?? []));
    if (t.type === "codespan") return chalk.cyan(t.text ?? "");
    if (t.type === "link") return renderInline(t.tokens ?? []) + " " + chalk.gray(`(${t.href})`);
    if (t.type === "br") return "\n";
    if (Array.isArray(t.tokens)) return renderInline(t.tokens);
    return t.text ?? t.raw ?? "";
  }).join("");
}

function renderTable(token: any, width: number): string {
  const rows = [token.header ?? [], ...(token.rows ?? [])];
  const cellText: string[][] = rows.map((row: any[]) =>
    row.map((cell) => renderInline(cell.tokens ?? [{type: "text", text: cell.text ?? ""}]))
  );
  const colCount = Math.max(...cellText.map((r) => r.length), 0);
  if (colCount === 0) return "";
  // renderInline returns ANSI-colored text; measure/pad using the visible
  // length (escape codes stripped) so columns line up regardless of color.
  const stripAnsi = (s: string) => s.replace(/\u001B\[[0-9;]*m/g, "");
  const visLen = (s: string) => stripAnsi(s).length;
  const pad = (s: string, w: number) => s + " ".repeat(Math.max(0, w - visLen(s)));
  const clip = (s: string, w: number) => {
    if (visLen(s) <= w) return s;
    // Truncate on the stripped text, then re-apply nothing (we keep it simple
    // and clip the raw string conservatively by visible chars).
    let out = "";
    let count = 0;
    let i = 0;
    while (i < s.length && count < w - 1) {
      if (s[i] === "\u001B") { // copy whole escape sequence without counting
        const m = s.slice(i).match(/^\u001B\[[0-9;]*m/);
        if (m) { out += m[0]; i += m[0].length; continue; }
      }
      out += s[i]; count++; i++;
    }
    return out + "…";
  };

  // Distribute available width across columns, capping each at its content.
  const border = 3; // " │ " between cells
  const avail = Math.max(colCount * 4, width - 4);
  const maxCol = Math.max(6, Math.floor((avail - (colCount - 1) * border) / colCount));
  const widths = Array.from({length: colCount}, (_, col) =>
    Math.min(maxCol, Math.max(3, ...cellText.map((row) => visLen(row[col] ?? ""))))
  );

  const line = (l: string, m: string, r: string) =>
    chalk.gray(l + widths.map((w) => "─".repeat(w + 2)).join(m) + r);
  const fmtRow = (row: string[], bold: boolean) => {
    const cells = widths.map((w, col) => {
      const raw = clip(row[col] ?? "", w);
      const padded = pad(raw, w);
      return " " + (bold ? chalk.bold(padded) : padded) + " ";
    });
    return chalk.gray("│") + cells.join(chalk.gray("│")) + chalk.gray("│");
  };

  const lines: string[] = [];
  lines.push(line("┌", "┬", "┐"));
  lines.push(fmtRow(cellText[0] ?? [], true));
  lines.push(line("├", "┼", "┤"));
  for (let i = 1; i < cellText.length; i++) lines.push(fmtRow(cellText[i], false));
  lines.push(line("└", "┴", "┘"));
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function fmtNum(n: number | undefined): string {
  return Math.max(0, Math.floor(n ?? 0)).toLocaleString();
}

function messageFromPayload(raw: any): ChatItem[] {
  const role = String(raw?.role ?? "system");
  const content = String(raw?.content ?? "");
  const detail = String(raw?.detail ?? raw?.reasoningContent ?? raw?.reasoning_content ?? "");
  const id = String(raw?.id ?? `${Date.now()}-${Math.random()}`);
  const toolCalls = String(raw?.toolCallsJson ?? raw?.tool_calls_json ?? "");
  if (!content.trim() && !detail.trim() && !toolCalls.trim()) return [];
  if (role === "assistant") {
    const items: ChatItem[] = [];
    // Reasoning becomes its own dimmed item placed BEFORE the answer, matching
    // the live-stream layout (thinking above the final reply).
    if (detail.trim()) items.push({id: `${id}-think`, kind: "thinking", text: "Thinking", detail});
    items.push({id, kind: "agent", text: content || "(assistant response)"});
    return items;
  }
  if (role === "user") return [{id, kind: "user", text: content}];
  if (role === "tool") return [{id, kind: "tool", text: "tool result", detail: detail || content}];
  if (role === "thinking") return [{id, kind: "thinking", text: "Thinking", detail: detail || content}];
  return [{id, kind: "system", text: content}];
}

// ---------------------------------------------------------------------------
// Bridge (Unix socket JSON-RPC)
// ---------------------------------------------------------------------------

function connectBridge(onMessage: (msg: JsonMessage) => void, onError: (err: string) => void): Bridge {
  const addr = process.env.TIGERCLI_BRIDGE_ADDR ?? "";
  if (!addr.startsWith("unix:")) {
    onError("TIGERCLI_BRIDGE_ADDR is missing or unsupported.");
    return {send: () => {}, close: () => {}};
  }
  const socketPath = addr.slice("unix:".length);
  const socket = net.createConnection(socketPath);
  let buf = "";
  socket.on("data", (chunk) => {
    buf += chunk.toString("utf8");
    while (true) {
      const idx = buf.indexOf("\n");
      if (idx < 0) break;
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      try { onMessage(JSON.parse(line)); }
      catch { onError("Invalid bridge JSON received."); }
    }
  });
  socket.on("error", (err) => onError(err.message));
  socket.on("close", () => onError("Connection to engine lost. Restart TigerLiteCode."));
  socket.on("end", () => onError("Engine disconnected."));
  return {
    send(method, params) {
      if (socket.destroyed) return;
      try { socket.write(JSON.stringify({jsonrpc: "2.0", method, params}) + "\n"); }
      catch { /* socket already closed */ }
    },
    close() { socket.end(); },
  };
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

function StatusBar({busy, statusText, session, width, yoloMode, paused}: {
  busy: boolean; statusText: string; session: SessionInfo | null; width: number; yoloMode: boolean; paused?: boolean;
}) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!busy || paused) return;
    const id = setInterval(() => setTick((n) => n + 1), 80);
    return () => clearInterval(id);
  }, [busy, paused]);

  const cachePct = session?.cacheHitRate != null ? `${Math.round(session.cacheHitRate * 100)}%` : "-";
  const spinChar = SPINNER[tick % SPINNER.length];
  const spinColor = SPINNER_COLORS[tick % SPINNER_COLORS.length] as any;
  const model = session?.model || "not set";
  const provider = session?.provider || "-";
  const mode = session?.mode || "build";
  const effort = session?.reasoningEffort || "high";

  // Live context-usage indicator: current conversation tokens vs the
  // auto-compact threshold (the actionable limit). Shown compactly as e.g.
  // "120K/500K" so the user can see how close the conversation is to
  // auto-compacting.
  const fmtK = (n: number) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : `${n}`;
  const ctxTok = session?.contextTokens ?? 0;
  const ctxLimit = session?.compactSize ?? 500_000;
  const ctxStr = `${fmtK(ctxTok)}/${fmtK(ctxLimit)}`;

  // Compose the status as a SINGLE line and hard-truncate it to the terminal
  // width. A multi-segment row that wraps to 2+ lines makes the animated
  // dynamic frame taller than the terminal, which is what lets Ink stack
  // duplicate status bars in the scrollback. One non-wrapping line keeps the
  // frame a fixed single row.
  const segs = [
    busy ? (statusText || "running") : "idle",
    `${SYM.mode} ${mode}`,
    `${SYM.model} ${provider}/${model}`,
    `${SYM.effort} ${effort}`,
    `${SYM.ctx} ${ctxStr}`,
    `${SYM.cache} ${cachePct}`,
    `${SYM.cost} $${(session?.cost ?? 0).toFixed(4)}`,
    `${SYM.req} ${fmtNum(session?.requests)}`,
  ];
  const lead = (yoloMode ? `${SYM.yolo} YOLO ` : "") + (busy ? spinChar : SYM.idle) + " ";
  const title = session?.title || "TigerLiteCode";
  let line = `${lead}${segs[0]}  ⬡ ${title}  ${segs.slice(1).join("  ")}`;
  if (line.length > width) line = line.slice(0, Math.max(0, width));

  return (
    <Box width={width} flexShrink={0}>
      <Text color={busy ? spinColor : "green"} wrap="truncate-end">{line}</Text>
    </Box>
  );
}

function MenuView({title, hint, items, idx, footer}: {
  title: string; hint: string; items: MenuItem[]; idx: number; footer?: string;
}) {
  return (
    <Box flexDirection="column" flexGrow={1} borderStyle="single" borderColor={TC.accent} paddingX={1}>
      <Text color={TC.accent} bold>{title}</Text>
      <Text color="gray">{hint}</Text>
      {items.length === 0 ? <Text color="gray">No items.</Text> : null}
      {items.map((it, i) => (
        <Box key={`${it.value}-${i}`} flexDirection="column">
          <Text color={i === idx ? TC.accent : it.disabled ? "gray" : undefined}>
            {i === idx ? "> " : "  "}{it.label}
          </Text>
          {i === idx && it.hint ? <Text color="gray">    {it.hint}</Text> : null}
        </Box>
      ))}
      {footer ? <Text color="gray">{footer}</Text> : null}
    </Box>
  );
}

function InfoView({title, lines, width}: {title: string; lines: string[]; width: number}) {
  return (
    <Box flexDirection="column" borderStyle="single" borderColor={TC.accent} paddingX={1} width={width}>
      <Text color={TC.accent} bold>{title}</Text>
      <Box flexDirection="column" marginTop={1} width={Math.max(10, width - 4)}>
        {lines.map((line, i) => <Text key={i} wrap="wrap">{line}</Text>)}
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Welcome Screen
// ---------------------------------------------------------------------------

function WelcomeScreen({projectPath, session, providers, width}: {
  projectPath: string; session: SessionInfo | null; providers: ProviderInfo[]; width: number;
}) {
  const logoColor = (line: string, i: number) => {
    // Tiger gradient: bright orange at the top fading to deep burnt orange.
    const colors = ["#ffb02e", "#ffa42a", "#ff8c1a", "#fb7d14", "#f06a14", "#e2570f", "#d14e0c", "#bf450a"];
    return colors[Math.min(i, colors.length - 1)] ?? "#bf450a";
  };
  const logoWidth = 80;
  const logoStart = Math.max(0, Math.floor((width - logoWidth) / 2));

  const modelInfo = session?.model
    ? `${session.provider ?? "-"}/${session.model}`
    : "No model configured — use /config to set up a provider";

  return (
    <Box flexDirection="column" paddingY={1} width={width}>
      {/* Logo */}
      <Box flexDirection="column" marginBottom={1}>
        {TIGER_LOGO.map((line, i) => (
          <Text key={i} color={logoColor(line, i) as any}>
            {" ".repeat(Math.max(0, logoStart))}{line}
          </Text>
        ))}
      </Box>
      {/* Welcome header */}
      <Box flexDirection="column" marginY={1} width={Math.max(40, width - 8)} marginX={4}>
        <Text bold color={TC.accent}>Welcome to TigerLiteCode</Text>
        <Text dimColor>Your AI-powered coding assistant in the terminal</Text>
        <Box marginTop={1} flexDirection="column">
          {projectPath ? <Text>Project: <Text color="green">{projectPath}</Text></Text> : null}
          <Text>Model: <Text color={TC.accent}>{modelInfo}</Text></Text>
          {providers.length > 0 ? (
            <Box flexDirection="column">
              <Text>Configured: <Text color="green">{providers.filter((p) => p.configured).map((p) => p.id).join(", ") || "none"}</Text></Text>
              <Text dimColor>Available: {providers.map((p) => p.id).join(", ")}</Text>
            </Box>
          ) : null}
        </Box>
      </Box>
      {/* Quick tips — flat (no border). Bordered boxes leave fragments when the
          slash menu reflows the layout beneath them, so the whole UI stays
          frameless. */}
      <Box flexDirection="column" marginX={4} marginTop={1} width={Math.max(40, width - 8)}>
        <Text bold color="yellow">Quick Start</Text>
        <Text dimColor>Type a message and press Enter to start coding with AI.</Text>
        <Box marginTop={1} flexDirection="column">
          <Text><Text color={TC.accent}>/help</Text>      Show all commands</Text>
          <Text><Text color={TC.accent}>/sessions</Text>  Resume a previous session</Text>
          <Text><Text color={TC.accent}>/config</Text>    Configure API keys and models</Text>
          <Text><Text color={TC.accent}>/model</Text>     Choose an AI model</Text>
          <Text><Text color={TC.accent}>/new</Text>       Start a fresh session</Text>
          <Text><Text color={TC.accent}>Ctrl+C</Text>     Interrupt current agent turn</Text>
          <Text><Text color={TC.accent}>Ctrl+Q</Text>     Quit TigerLiteCode</Text>
        </Box>
      </Box>
      {/* Tip */}
      <Box marginX={4} marginTop={1} width={Math.max(40, width - 8)}>
        <Text dimColor>» Tip: Use @ to mention files, / to see commands, ↑↓ for history</Text>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Slash Command Menu
// ---------------------------------------------------------------------------

type SlashItem = {label: string; desc: string; cmd: string};

const SLASH_COMMANDS: SlashItem[] = [
  {label: "/sessions", desc: "List & resume previous sessions", cmd: "sessions"},
  {label: "/new", desc: "Start a fresh session in this project", cmd: "new"},
  {label: "/init", desc: "Create AGENTS.md instruction file", cmd: "init"},
  {label: "/compact", desc: "Compact conversation history", cmd: "compact"},
  {label: "/compact_size", desc: "Set auto-compact token threshold (e.g. 500k)", cmd: "compact_size"},
  {label: "/context", desc: "Show token usage and context info", cmd: "context"},
  {label: "/model", desc: "Choose an AI model", cmd: "model"},
  {label: "/provider", desc: "Choose a provider", cmd: "provider"},
  {label: "/effort", desc: "Set reasoning effort (low/medium/high/max)", cmd: "effort"},
  {label: "/config", desc: "Configure API keys and models", cmd: "config"},
  {label: "/cache", desc: "Show cache stats, tokens, and cost", cmd: "cache"},
  {label: "/help", desc: "Show help and keybindings", cmd: "help"},
  {label: "/mode", desc: "Toggle build/plan mode", cmd: "mode"},
  {label: "/yolo", desc: "Toggle YOLO mode (auto-approve tools)", cmd: "yolo"},
  {label: "/exit", desc: "Quit TigerLiteCode", cmd: "exit"},
];

function SlashCommandMenu({query, idx, width}: {query: string; idx: number; width: number}) {
  const matches = query
    ? SLASH_COMMANDS.filter((c) => c.label.toLowerCase().includes(query.toLowerCase()))
    : SLASH_COMMANDS;

  const safeIdx = Math.min(idx, Math.max(0, matches.length - 1));

  // Flat (no border) menu. Bordered boxes leave stale glyphs/border fragments
  // when the menu grows/shrinks over the content above it. Each row is a single
  // fixed-width, truncated string so it fully overwrites its column every frame.
  const boxWidth = Math.max(24, Math.min(54, width - 2));
  const lineWidth = boxWidth - 2;

  return (
    <Box flexDirection="column" width={boxWidth} marginX={1}>
      <Box width={lineWidth}>
        <Text color={TC.accent} bold wrap="truncate">Commands</Text>
        <Text color="gray" wrap="truncate">{`  ↑↓ select · Enter run · Esc dismiss`}</Text>
      </Box>
      {matches.length === 0 ? (
        <Box width={lineWidth}><Text color="gray" wrap="truncate">No matching commands</Text></Box>
      ) : (
        matches.map((cmd, i) => {
          const active = i === safeIdx;
          // ONE text node per row. Two sibling <Text> in a flex row can leave
          // stale glyphs from the previous frame (overlapping label/desc), so
          // we compose a single fixed-width string and truncate it as a whole.
          const marker = active ? "> " : "  ";
          const labelCol = cmd.label.length > 12 ? cmd.label.slice(0, 12) : cmd.label.padEnd(12, " ");
          let row = `${marker}${labelCol}  ${cmd.desc}`;
          if (row.length > lineWidth) row = row.slice(0, lineWidth);
          return (
            <Box key={cmd.cmd} width={lineWidth}>
              <Text color={active ? TC.accent : "gray"} bold={active} wrap="truncate">
                {row}
              </Text>
            </Box>
          );
        })
      )}
      <Box width={lineWidth}>
        <Text color="gray" wrap="truncate">{`${safeIdx + 1}/${matches.length}`}</Text>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Exit Summary
// ---------------------------------------------------------------------------

function ExitSummary({session, width}: {session: SessionInfo | null; width: number}) {
  const s = session;
  const cachePct = s?.cacheHitRate != null ? `${Math.round(s.cacheHitRate * 100)}%` : "-";
  const boxWidth = Math.min(60, width - 4);

  return (
    <Box flexDirection="column" width={width} paddingY={1}>
      <Box flexDirection="column" borderStyle="single" borderColor={TC.accent} paddingX={2} width={boxWidth} marginX={Math.max(0, Math.floor((width - boxWidth) / 2))}>
        <Text bold color={TC.accent}>Session Summary</Text>
        <Box marginTop={1} flexDirection="column">
          <Text>Session:  <Text color={TC.accent}>{s?.id ?? "(none)"}</Text></Text>
          <Text>Title:    <Text color={TC.accent}>{s?.title ?? "-"}</Text></Text>
          <Text>Model:    <Text color={TC.accent}>{s?.provider ?? "-"}/{s?.model ?? "-"}</Text></Text>
        </Box>
        <Box marginTop={1} flexDirection="column">
          <Text>Tokens in:   <Text color="yellow">{fmtNum(s?.tokensIn)}</Text></Text>
          <Text>Tokens out:  <Text color="yellow">{fmtNum(s?.tokensOut)}</Text></Text>
          <Text>Cache hits:  <Text color="green">{fmtNum(s?.cacheHitTokens)}</Text></Text>
          <Text>Cache miss:  <Text color="gray">{fmtNum(s?.cacheMissTokens)}</Text></Text>
          <Text>Cache rate:  <Text color="green">{cachePct}</Text></Text>
        </Box>
        <Box marginTop={1} flexDirection="column">
          <Text>Requests:   <Text color={TC.accent}>{fmtNum(s?.requests)}</Text></Text>
          <Text>Cost (USD): <Text color="yellow">${(s?.cost ?? 0).toFixed(4)}</Text></Text>
        </Box>
      </Box>
      <Box marginTop={1} width={width}>
        <Text dimColor>{"  "}Thanks for using TigerLiteCode!</Text>
      </Box>
    </Box>
  );
}

function MessageItem({item, width, focused, expandedThinkingId, tick}: {
  item: ChatItem; width: number; focused: boolean; expandedThinkingId: string | null; tick: number;
}) {
  if (item.kind === "thinking" && !item.detail && !item.streaming) return null;

  const sym =
    item.kind === "user"     ? SYM.user :
    item.kind === "agent"    ? SYM.agent :
    item.kind === "thinking" ? SYM.thinking :
    item.kind === "tool"     ? SYM.tool :
    item.kind === "approval" ? SYM.approval :
    SYM.system;

  // For tool items: the tool NAME comes from item.text ("read ok"), and the
  // human PARAMS summary (file path, glob, command, query) from item.summary.
  // Show both beside the label so a collapsed tool line reads e.g.
  // "tool read src/index.tsx · ok".
  const toolName =
    item.kind === "tool" && item.text
      ? item.text.replace(/\s+(running|ok|stopped|denied)$/i, "").trim()
      : "";
  const toolSummary =
    item.kind === "tool"
      ? (toolName + (item.summary ? " " + item.summary : "")).trim()
      : "";
  const toolStatus =
    item.kind === "tool"
      ? (item.streaming ? "running" : /denied$/i.test(item.text) ? "denied" : /(ok)$/i.test(item.text) ? "ok" : /stopped$/i.test(item.text) ? "stopped" : "")
      : "";

  const label =
    item.kind === "agent"    ? "agent" :
    item.kind === "approval" ? "approval" :
    item.kind === "thinking" ? "thinking" :
    // Tool name + params summary are rendered separately, so the literal
    // "tool" label is omitted to keep the header compact.
    item.kind === "tool"     ? "" :
    item.kind;

  const labelColor =
    item.kind === "user"     ? TC.user :
    item.kind === "agent"    ? TC.agent :
    item.kind === "thinking" ? TC.thinking :
    item.kind === "tool"     ? TC.tool :
    item.kind === "approval" ? "yellow" : "gray";

  // Two-column hierarchy (deepcode-cli style): a narrow gutter column holds the
  // marker, a flexible content column holds the label header and body. Wrapped
  // body lines stay aligned under the content column instead of drifting under
  // the marker, which keeps the transcript hierarchy clean and readable.
  const GUTTER = 2;
  const contentWidth = Math.max(20, width - GUTTER - 1);

  const isThinkingCollapsed = item.kind === "thinking" && item.id !== expandedThinkingId && !focused;
  const isToolCollapsed = item.kind === "tool" && !item.streaming && !focused;
  const summaryOnly = isThinkingCollapsed || isToolCollapsed;

  // While a tool is still streaming it lives in the dynamic frame, which Ink
  // redraws every tick. A tall body (big diff / detail) there makes the frame
  // overflow the viewport and the transcript overlaps. So keep streaming tool
  // bodies compact; the full diff/detail renders once the item finalizes and
  // commits into the static scrollback below.
  const liveCompact = item.kind === "tool" && !!item.streaming;
  const compactBody = summaryOnly || liveCompact;

  const showDetail = !!item.detail && !summaryOnly;

  const streamAnim = item.streaming ? " " + STREAM_DOTS[tick % STREAM_DOTS.length] : "";
  const expandHint = (item.detail && (item.kind === "tool" || item.kind === "thinking"))
    ? (summaryOnly ? ` ${SYM.collapse}` : ` ${SYM.expand}`)
    : "";
  const gutter = focused ? SYM.focus : sym;

  // User messages get a distinct card so the person's turn is easy to pick out
  // from agent/thinking/tool blocks. We use a bordered box (Codex/ink style)
  // rather than background-filled lines: Ink draws and wraps the box cleanly at
  // any width, where manual per-line background padding leaves ragged gaps.
  if (item.kind === "user") {
    // Span the full available width so multi-line / long pasted text wraps with
    // room inside the border. Ink wraps the inner <Text> to the box width minus
    // borders + horizontal padding automatically.
    const cardW = Math.max(20, Math.min(width - 1, contentWidth + 2));
    return (
      <Box
        flexDirection="column"
        marginBottom={1}
        width={cardW}
        borderStyle="round"
        borderColor={TC.user}
        paddingX={1}
      >
        <Text color={TC.user} bold>user</Text>
        {item.text.split("\n").map((ln, i) => (
          <Text key={i} color={TC.user} wrap="wrap">{ln.length ? ln : " "}</Text>
        ))}
      </Box>
    );
  }

  return (
    <Box flexDirection="row" marginLeft={1} marginBottom={1} width={width} gap={1}>
      {/* Gutter column: the marker only. */}
      <Box width={GUTTER} flexShrink={0}>
        <Text color={labelColor as any} bold>{gutter}</Text>
      </Box>
      {/* Content column: header line + body, all left-aligned together. */}
      <Box flexDirection="column" flexGrow={1} width={contentWidth}>
        <Text wrap="truncate-end">
          {label ? <Text color={labelColor as any} bold>{label}</Text> : null}
          {item.kind === "tool" && toolSummary ? <Text color={labelColor as any}>{label ? " " : ""}{toolSummary}</Text> : null}
          {item.kind === "tool" && toolStatus ? <Text color="gray">{` · ${toolStatus}`}</Text> : null}
          {item.streaming && item.kind !== "tool" ? <Text color={TC.thinking}>{streamAnim}</Text> : null}
          {expandHint ? <Text color="gray">{expandHint}</Text> : null}
        </Text>
        {summaryOnly
          ? null
          : item.kind === "thinking"
            ? <Text color="gray" wrap="wrap">{mdToAnsi(item.detail as string, contentWidth)}</Text>
            : <Text wrap="wrap">{mdToAnsi(item.streaming ? tailLines(item.text, 14) : item.text, contentWidth)}</Text>}
        {/* Diff preview for write/edit: rendered like modern coding TUIs.
            Stays visible even when the tool row is collapsed (the diff is the
            meaningful result), but collapses to a compact head when not focused
            so a large write doesn't flood the transcript. */}
        {item.kind === "tool" && item.diff ? (() => {
          const full = item.diff!.diff;
          const COLLAPSED_MAX = 12;
          const shown = compactBody && full.length > COLLAPSED_MAX
            ? full.slice(0, COLLAPSED_MAX)
            : full;
          const hidden = full.length - shown.length;
          return (
          <Box flexDirection="column">
            <Text color="gray">
              {`${item.diff!.new_file ? "new file " : ""}${item.diff!.path}  `}
              <Text color={TC.success}>{`+${item.diff!.added}`}</Text>
              <Text color="gray">{" "}</Text>
              <Text color={TC.error}>{`-${item.diff!.removed}`}</Text>
            </Text>
            {shown.map((ln, i) => {
              const mark = ln.charAt(0);
              const body = ln.slice(1);
              const color = mark === "+" ? TC.success : mark === "-" ? TC.error : mark === "@" ? TC.plan : "gray";
              const prefix = mark === "+" ? "+" : mark === "-" ? "-" : mark === "@" ? "" : " ";
              return (
                <Text key={i} color={color} wrap="truncate-end">{prefix}{body}</Text>
              );
            })}
            {hidden > 0 ? <Text color="gray">{`… +${hidden} more diff lines`}</Text> : null}
          </Box>
          );
        })() : null}
        {showDetail && item.kind !== "thinking" && !(item.kind === "tool" && item.diff)
          ? <Text color="gray" wrap="wrap">{mdToAnsi(clampDetail(liveCompact ? tailLines(item.detail as string, 8) : (item.detail as string), item.kind), contentWidth)}</Text>
          : null}
      </Box>
    </Box>
  );
}

function PromptLine({buffer, width, placeholder, configPrompt}: {
  buffer: PromptBuffer; width: number; placeholder: string; configPrompt?: string;
}) {
  const prefix = configPrompt || "$ ";
  const isEmpty = !buffer.text;

  if (isEmpty) {
    // Show cursor at start with placeholder dimmed after it
    return (
      <Box flexDirection="row" width={width}>
        <Text color={TC.accent}>{prefix}</Text>
        <Text backgroundColor="cyan" color="black" bold> </Text>
        <Text color="gray"> {placeholder}</Text>
      </Box>
    );
  }

  // Render one row per source line so embedded newlines from pasted text are
  // preserved exactly — a single <Text> with "\n" inside gets its lines
  // collapsed/clipped by Ink's layout when combined with wrapping. The cursor
  // cell is drawn on the line/column it actually sits on.
  const displayText = buffer.text;
  const cursor = Math.min(buffer.cursor, displayText.length);
  const lines = displayText.split("\n");

  // Locate the cursor's (row, col) within the split lines.
  let cRow = 0;
  let cCol = cursor;
  for (let i = 0; i < lines.length; i++) {
    if (cCol <= lines[i].length) { cRow = i; break; }
    cCol -= lines[i].length + 1; // +1 for the consumed "\n"
    cRow = i + 1;
  }

  return (
    <Box flexDirection="column" width={width}>
      {lines.map((ln, i) => {
        const isCursorRow = i === cRow;
        const rowPrefix = i === 0 ? prefix : "  ";
        if (!isCursorRow) {
          return (
            <Text key={i} wrap="wrap">
              <Text color={TC.accent}>{rowPrefix}</Text>
              <Text>{ln.length ? ln : " "}</Text>
            </Text>
          );
        }
        const before = ln.slice(0, cCol);
        const at = ln[cCol] ?? " ";
        const after = ln.slice(cCol + 1);
        return (
          <Text key={i} wrap="wrap">
            <Text color={TC.accent}>{rowPrefix}</Text>
            <Text>{before}</Text>
            <Text backgroundColor="cyan" color="black" bold>{at}</Text>
            <Text>{after}</Text>
          </Text>
        );
      })}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------

function App() {
  const {exit} = useApp();
  const {stdout} = useStdout();
  const {isRawModeSupported} = useStdin();
  const [termSize, setTermSize] = useState(() => sanitizeSize(stdout?.columns, stdout?.rows));
  const width = termSize.width;
  const height = termSize.height;
  // Bumped whenever the terminal width changes. A width change reflows every
  // wrapped row, so the already-flushed <Static> output (printed at the old
  // width) becomes stale. We clear the screen and force <Static> to re-flush
  // the entire transcript at the new width — the same approach deepcode-cli
  // uses (full clear + reset of the static list on column change).
  const [redrawEpoch, setRedrawEpoch] = useState(0);
  const lastColsRef = useRef<number | null>(null);
  // When a resize triggers a redraw, we empty <Static> for a single tick so it
  // resets its internal "already-printed" index, then restore the full list on
  // the next tick — re-flushing the entire transcript at the new width with no
  // stale wrapped rows or duplicated frames.
  const [staticSuspended, setStaticSuspended] = useState(false);
  useEffect(() => {
    if (redrawEpoch === 0) return;
    setStaticSuspended(true);
    const id = setTimeout(() => setStaticSuspended(false), 0);
    return () => clearTimeout(id);
  }, [redrawEpoch]);

  // Replace the whole transcript (session switch / resume / new). <Static> keeps
  // everything it has already printed in the terminal scrollback, so simply
  // calling setItems() would leave the previous session's lines stacked above
  // the new ones. We clear the screen + scrollback and bump the redraw epoch so
  // <Static> resets its printed index and re-flushes only the new items.
  const resetTranscript = useCallback((next: ChatItem[]) => {
    stdout?.write("\u001B[2J\u001B[3J\u001B[H");
    setRedrawEpoch((n) => n + 1);
    setItems(next);
  }, [stdout]);

  // Clear the screen + scrollback and re-flush the committed transcript without
  // changing the items. Used after dismissing a full-width colored overlay (the
  // YOLO terms banner) so its background-colored cells don't get left behind in
  // scrollback when the dynamic region shrinks.
  const forceRedraw = useCallback(() => {
    stdout?.write("\u001B[2J\u001B[3J\u001B[H");
    setRedrawEpoch((n) => n + 1);
  }, [stdout]);

  // Listen for terminal resize. Sanitize + debounce so a burst of resize events
  // (or a transient 0/undefined size mid-drag) never produces negative layout
  // math or a flicker/blank frame.
  useEffect(() => {
    if (!stdout) return;
    let raf: ReturnType<typeof setTimeout> | null = null;
    const apply = () => {
      const next = sanitizeSize(stdout.columns, stdout.rows);
      setTermSize((prev) => (prev.width === next.width && prev.height === next.height ? prev : next));
      if (lastColsRef.current === null) {
        lastColsRef.current = next.width;
      } else if (lastColsRef.current !== next.width) {
        lastColsRef.current = next.width;
        // Full clear + scroll-region reset, then re-flush <Static> at new width.
        stdout.write("\u001B[2J\u001B[3J\u001B[H");
        setRedrawEpoch((n) => n + 1);
      }
    };
    const onResize = () => {
      if (raf) clearTimeout(raf);
      raf = setTimeout(apply, 50);
    };
    stdout.on("resize", onResize);
    apply(); // ensure we start from the real current size
    return () => {
      if (raf) clearTimeout(raf);
      stdout.off("resize", onResize);
    };
  }, [stdout]);
  const bridge = useRef<Bridge | null>(null);

  const turnRef = useRef(0);
  // Unique id for the assistant turn currently streaming. A fresh id is minted
  // when the backend signals `busy=true`, guaranteeing tokens/reasoning from a
  // new turn never append onto the previous turn's message item. Cleared when
  // the turn finishes (`done`/`busy=false`).
  const streamTurnRef = useRef<string | null>(null);
  // Strict chronological ordering within a turn: each streaming segment gets a
  // unique, monotonically increasing id. When a tool call or a thinking block
  // interrupts the stream, the open agent/think segment is closed (set null) so
  // the NEXT token/reasoning starts a fresh block appended AFTER the interrupter
  // — instead of merging back into an earlier block that sits above it.
  const openAgentIdRef = useRef<string | null>(null);
  const openThinkIdRef = useRef<string | null>(null);
  const segCounterRef = useRef(0);
  // Coalesce high-frequency stream chunks. Tokens/reasoning can arrive hundreds
  // of times per second; calling setItems on each one floods Ink with redraws
  // of the live frame and the transcript visibly glitches/overlaps. Instead we
  // accumulate chunks per segment id here and flush them to React on a short
  // timer (~1 frame), collapsing many updates into a few. Flushed immediately
  // on segment boundaries (tool_call / done) so chronological order is exact.
  const pendingTextRef = useRef<Map<string, {kind: "agent" | "thinking"; text: string}>>(new Map());
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const historyRef = useRef<string[]>([]);
  const historyIdxRef = useRef(-1);
  const pendingPasteRef = useRef<string | null>(null); // null = not in paste, string = accumulating

  const [items, setItems] = useState<ChatItem[]>([]);
  const [animTick, setAnimTick] = useState(0);
  const [buffer, setBuffer] = useState<PromptBuffer>(EMPTY);
  // Data URLs of images pasted from the clipboard (Ctrl+V), attached to the
  // next submitted message. Cleared after send or with Ctrl+X.
  const [pendingImages, setPendingImages] = useState<string[]>([]);
  const pendingImagesRef = useRef<string[]>(pendingImages);
  pendingImagesRef.current = pendingImages;
  const [busy, setBusy] = useState(false);
  // Live mirror of `busy`. submitChat is memoized without `busy` in its deps and
  // is also invoked through a ref from the raw stdin handler, so reading the
  // state value directly would be stale. The ref always reflects the latest
  // value, so the "queue while busy vs send now" decision is correct.
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const [statusText, setStatusText] = useState("");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsScope, setSessionsScope] = useState<"project" | "all">("project");
  const [projectPath, setProjectPath] = useState("");
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [view, setView] = useState<View>("chat");
  const [menuIdx, setMenuIdx] = useState(0);
  const [permissionId, setPermissionId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [scrollIdx, setScrollIdx] = useState(0);
  const [configTarget, setConfigTarget] = useState<{provider: string; step: "apiKey" | "models"} | null>(null);
  const [configApiKeyTemp, setConfigApiKeyTemp] = useState("");
  // When a connected provider is selected, hold it here for the small
  // "use saved key vs re-enter key" choice menu.
  const [providerActionTarget, setProviderActionTarget] = useState<string>("");
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [showWelcome, setShowWelcome] = useState(true);
  const [slashMenuIdx, setSlashMenuIdx] = useState(0);
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);
  const [yoloMode, setYoloMode] = useState(false);
  // Live ref so the long-lived bridge message handler reads the current YOLO
  // state instead of the value captured when the effect first ran.
  const yoloModeRef = useRef(yoloMode);
  yoloModeRef.current = yoloMode;
  const [showYoloTOS, setShowYoloTOS] = useState(false);

  const append = useCallback((item: ChatItem) => setItems((prev) => [...prev, item]), []);

  // Apply all buffered stream chunks to the transcript in a single batched
  // update, creating segment items on first sight and appending to existing
  // ones. Called on a short timer while streaming and synchronously at segment
  // boundaries. Safe to call when the buffer is empty (no-op).
  const flushStream = useCallback(() => {
    if (flushTimerRef.current) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const pending = pendingTextRef.current;
    if (pending.size === 0) return;
    const entries = Array.from(pending.entries());
    pendingTextRef.current = new Map();
    setItems((prev) => {
      let next = prev;
      let mutated = false;
      for (const [id, buf] of entries) {
        const idx = next.findIndex((it) => it.id === id);
        if (idx >= 0) {
          if (!mutated) { next = next.slice(); mutated = true; }
          const it = next[idx];
          next[idx] = buf.kind === "thinking"
            ? {...it, detail: (it.detail ?? "") + buf.text, streaming: true}
            : {...it, text: it.text + buf.text, streaming: true};
        } else {
          if (!mutated) { next = next.slice(); mutated = true; }
          next.push(buf.kind === "thinking"
            ? {id, kind: "thinking", text: "Thinking", detail: buf.text, streaming: true}
            : {id, kind: "agent", text: buf.text, streaming: true});
        }
      }
      return mutated ? next : prev;
    });
  }, []);

  // Buffer a stream chunk and ensure a near-term flush is scheduled.
  const scheduleStream = useCallback((id: string, kind: "agent" | "thinking", text: string) => {
    const pending = pendingTextRef.current;
    const cur = pending.get(id);
    if (cur) cur.text += text;
    else pending.set(id, {kind, text});
    if (!flushTimerRef.current) {
      flushTimerRef.current = setTimeout(() => { flushStream(); }, 33);
    }
  }, [flushStream]);

  // Auto-dismiss status messages
  useEffect(() => {
    if (!statusMsg) return;
    const t = setTimeout(() => setStatusMsg(null), 2500);
    return () => clearTimeout(t);
  }, [statusMsg]);

  // Global animation tick for streaming indicators in message items
  const hasStreaming = items.some((it) => it.streaming);
  // Pause all spinner/streaming animation while an approval prompt is pending.
  // Otherwise the live region (streaming thinking line) and the status bar keep
  // redrawing under the prompt, which reads as flicker/glitching while the user
  // is trying to answer the approval.
  const animPaused = permissionId !== null || showYoloTOS;
  useEffect(() => {
    if (!hasStreaming || animPaused) return;
    const id = setInterval(() => setAnimTick((n) => n + 1), 150);
    return () => clearInterval(id);
  }, [hasStreaming, animPaused]);

  // Terminal setup
  useEffect(() => {
    // Use the NORMAL screen (not the alternate buffer) so finalized messages
    // printed via <Static> stay in the terminal's native scrollback and the
    // user can scroll up to see earlier turns. The alt-screen would discard
    // scrollback and clip the transcript to a fixed-size canvas.
    stdout?.write(HIDE_CURSOR);
    if (isRawModeSupported) {
      stdout?.write(BRACKETED_PASTE_ON + EXTENDED_KEYS_ON);
    }
    return () => {
      stdout?.write(SHOW_CURSOR);
      if (isRawModeSupported) {
        stdout?.write(BRACKETED_PASTE_OFF + EXTENDED_KEYS_OFF);
      }
    };
  }, [stdout, isRawModeSupported]);

  // Bridge connection
  useEffect(() => {
    bridge.current = connectBridge((msg) => {
      const p = msg.params ?? {};
      if (msg.method === "config") {
        setSession((s) => ({...(s ?? {}), provider: p.provider, model: p.model, mode: p.mode, reasoningEffort: p.reasoningEffort}));
        if (p.cwd) setProjectPath(String(p.cwd));
      } else if (msg.method === "providers") {
        setProviders(Array.isArray(p.providers) ? p.providers : []);
      } else if (msg.method === "session") {
        setSession(p);
      } else if (msg.method === "sessions") {
        setSessions(Array.isArray(p.sessions) ? p.sessions : []);
        if (p.scope === "all" || p.scope === "project") setSessionsScope(p.scope);
      } else if (msg.method === "session_resume" || msg.method === "messages") {
        const next = (p.messages ?? []).flatMap(messageFromPayload) as ChatItem[];
        if (p.session) setSession(p.session);
        resetTranscript(next);
        setSelectedId(null);
        setScrollIdx(0);
        setShowWelcome(next.length === 0);
      } else if (msg.method === "message") {
        const newItems = messageFromPayload(p);
        if (newItems.length) setItems((prev) => [...prev, ...newItems]);
      } else if (msg.method === "token") {
        // Ensure a turn id exists even if a token arrives before `busy`.
        if (!streamTurnRef.current) streamTurnRef.current = `t${Date.now()}-${turnRef.current}`;
        // A token closes any open thinking segment, then continues (or opens) an
        // agent segment. Opening a NEW agent id after an interruption keeps the
        // continuation in arrival order, below the tool/thinking that preceded it.
        openThinkIdRef.current = null;
        if (!openAgentIdRef.current) {
          segCounterRef.current += 1;
          openAgentIdRef.current = `agent-${streamTurnRef.current}-${segCounterRef.current}`;
        }
        const id = openAgentIdRef.current;
        scheduleStream(id, "agent", String(p.content ?? ""));
      } else if (msg.method === "thinking") {
        turnRef.current += 1;
        setStatusText("Thinking");
      } else if (msg.method === "reasoning") {
        if (!streamTurnRef.current) streamTurnRef.current = `t${Date.now()}-${turnRef.current}`;
        // Reasoning closes any open agent segment and continues (or opens) a
        // thinking segment, so a later answer becomes a new block beneath it.
        openAgentIdRef.current = null;
        if (!openThinkIdRef.current) {
          segCounterRef.current += 1;
          openThinkIdRef.current = `think-${streamTurnRef.current}-${segCounterRef.current}`;
        }
        const id = openThinkIdRef.current;
        scheduleStream(id, "thinking", String(p.content ?? ""));
      } else if (msg.method === "tool_call") {
        // Land any buffered stream text before appending the tool so the tool
        // row stays in correct chronological order below the preceding text.
        flushStream();
        const tname = String(p.name ?? "tool");
        const tsum = String(p.summary ?? "");
        setStatusText(`${tname}${tsum ? " " + tsum : ""}`);
        // A tool interrupts the stream: close both open segments so any tokens
        // or reasoning that follow start fresh blocks AFTER this tool.
        openAgentIdRef.current = null;
        openThinkIdRef.current = null;
        append({id: String(p.id), kind: "tool", text: `${tname} running`, summary: tsum, detail: JSON.stringify(p.arguments ?? {}, null, 2), diff: (p.diff ?? null) as ChatItem["diff"], streaming: true});
      } else if (msg.method === "tool_result") {
        flushStream();
        const tname = String(p.name ?? "tool");
        const ok = String(p.permission) !== "deny";
        setItems((prev) => prev.map((it) => it.id === String(p.id)
          ? {...it, text: `${tname} ${ok ? "ok" : "denied"}`, summary: it.summary || String(p.summary ?? ""), detail: String(p.result ?? ""), streaming: false}
          : it));
      } else if (msg.method === "permission_required") {
        flushStream();
        const permId = String(p.id);
        const tool = String(p.tool || "");
        // YOLO mode: auto-approve non-bash tools
        if (yoloModeRef.current && tool !== "bash") {
          bridge.current?.send("permission_response", {toolCallId: permId, approved: true, always: false});
          append({id: permId, kind: "system", text: `▲ YOLO: auto-approved ${tool}`});
        } else {
          setPermissionId(permId);
          append({id: permId, kind: "approval", text: `${p.prompt ?? "Allow action?"}`});
        }
      } else if (msg.method === "busy") {
        flushStream();
        setBusy(Boolean(p.busy));
        setStatusText(p.busy ? String(p.text ?? "Working") : "");
        if (p.busy) {
          // Start a brand-new assistant turn. Minting a fresh id here is what
          // prevents a new reply from merging into the previous turn's block.
          turnRef.current += 1;
          streamTurnRef.current = `t${Date.now()}-${turnRef.current}`;
          openAgentIdRef.current = null;
          openThinkIdRef.current = null;
        } else {
          streamTurnRef.current = null;
          openAgentIdRef.current = null;
          openThinkIdRef.current = null;
          setItems((prev) => prev.map((it) => it.streaming ? {...it, streaming: false, text: it.kind === "tool" ? it.text.replace(/ running$/, " stopped") : it.text} : it));
        }
      } else if (msg.method === "done") {
        flushStream();
        setBusy(false);
        setStatusText("");
        streamTurnRef.current = null;
        openAgentIdRef.current = null;
        openThinkIdRef.current = null;
        setItems((prev) => prev.map((it) => it.streaming ? {...it, streaming: false, text: it.kind === "tool" ? it.text.replace(/ running$/, " stopped") : it.text} : it));
      } else if (msg.method === "error") {
        flushStream();
        setBusy(false);
        setStatusText("");
        append({id: `${Date.now()}`, kind: "system", text: `Error: ${p.message ?? ""}`});
      }
    }, (err) => append({id: `${Date.now()}`, kind: "system", text: `Bridge error: ${err}`}));
    return () => { if (flushTimerRef.current) clearTimeout(flushTimerRef.current); bridge.current?.close(); };
  }, [append]);

  // ---- Helpers ----

  const expandableIds = useMemo(
    () => items.filter((it) => it.kind === "tool" || (it.kind === "thinking" && it.detail)).map((it) => it.id),
    [items],
  );

  // Auto-collapse: keep only the last streaming thinking item expanded.
  // Once a non-thinking agent message arrives, all thinking collapses.
  const expandedThinkingId = useMemo(() => {
    let expanded: string | null = null;
    for (const item of items) {
      if (item.kind === "thinking") expanded = item.id;
      else if (item.kind === "agent" && !item.streaming) expanded = null;
    }
    return expanded;
  }, [items]);

  // Detect slash command token at start of buffer
  const slashToken = useMemo(() => {
    const text = buffer.text;
    // Only match at position 0, before any space
    if (text.startsWith("/")) {
      const spaceIdx = text.indexOf(" ");
      return spaceIdx > 0 ? text.slice(0, spaceIdx) : text;
    }
    return null;
  }, [buffer.text]);

  const slashMatches = useMemo(() => {
    if (!slashToken) return [];
    const q = slashToken.toLowerCase();
    return SLASH_COMMANDS.filter((c) => c.label.toLowerCase().includes(q));
  }, [slashToken]);

  const showSlashMenu = slashToken !== null && !busy && view === "chat" && !configTarget && !permissionId;

  // Reset slash menu index only when menu appears/disappears, not on every keystroke
  const prevSlashToken = useRef(slashToken);
  useEffect(() => {
    const wasActive = prevSlashToken.current !== null;
    const isActive = slashToken !== null;
    if (wasActive !== isActive) {
      setSlashMenuIdx(0);
    }
    prevSlashToken.current = slashToken;
  }, [slashToken]);

  // Auto-send queued message when agent finishes
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);
  useEffect(() => {
    if (!busy && queuedMessage) {
      const msg = queuedMessage;
      setQueuedMessage(null);
      setStatusMsg(null);
      const t = setTimeout(() => {
        if (!mountedRef.current) return;
        append({id: `${Date.now()}`, kind: "user", text: msg});
        setBusy(true);
        setStatusText("Working");
        bridge.current?.send("user_input", {text: msg, imageUrls: pendingImagesRef.current});
        setPendingImages([]);
      }, 100);
      return () => clearTimeout(t);
    }
  }, [busy, queuedMessage, append]);

  const menuItems: MenuItem[] = useMemo(() => {
    switch (view) {
      case "sessions":
        return sessions.map((s) => ({
          label: `${s.title || s.id}  ${s.provider || "-"}/${s.model || "-"}  ${String(s.updatedAt ?? "").slice(0, 10)}`,
          value: s.id ?? "",
          hint: s.id,
        }));
      case "provider":
        return providers.map((pr) => ({
          label: `${pr.id}${pr.configured ? "  ✓" : "  ✗"}`,
          value: pr.id,
        }));
      case "providerAction":
        return [
          {label: "Choose a model (use saved key)", value: "model"},
          {label: "Re-enter API key", value: "rekey"},
        ];
      case "model": {
        // Scope the model picker to the ACTIVE provider only. Mixing models
        // from every configured provider (e.g. opencode-zen + opencode-go) is
        // confusing — to switch providers the user goes through /provider.
        const activeProviderId = session?.provider || "";
        const allModels: MenuItem[] = [];
        for (const pr of providers) {
          if (!pr.configured) continue;
          // Only the active provider's models. If no active provider is known
          // yet, fall back to every configured provider so the list isn't empty.
          if (activeProviderId && pr.id !== activeProviderId) continue;
          const prModels = pr.models ?? [];
          if (prModels.length === 0) continue;
          for (const m of prModels) {
            // Strip provider prefix for cleaner display
            const cleanId = m.startsWith(pr.id + "/") ? m.slice(pr.id.length + 1) : m;
            allModels.push({
              label: `[${pr.id}] ${cleanId}`,
              // Encode the OWNING provider so selecting a model switches to its
              // provider, not the currently-active one.
              value: `${pr.id}::${m}`,
              hint: m === session?.model && pr.id === session?.provider ? "active" : undefined,
            });
          }
        }
        if (allModels.length === 0) {
          // No provider connected yet — guide the user to connect one first.
          if (!providers.some((pr) => pr.configured)) {
            return [{
              label: "No provider connected — run /provider to connect one first",
              value: "",
              disabled: true,
            }];
          }
          // Fallback: show raw current model if nothing else found
          return session?.model
            ? [{label: `${session.model}  (no models from backend)`, value: session.model}]
            : [{label: "No models available — run /provider to connect a provider", value: "", disabled: true}];
        }
        return allModels;
      }
      case "effort":
        return EFFORTS.map((e) => ({label: e, value: e, hint: e === session?.reasoningEffort ? "active" : undefined}));
      case "config":
        return providers.map((pr) => ({
          label: `${pr.id}${pr.configured ? "  ✓ configured" : "  ✗ not configured"}`,
          value: pr.id,
          hint: pr.configured ? "reconfigure" : "set API key + models",
        }));
      default: return [];
    }
  }, [view, sessions, providers, session]);

  const openView = useCallback((v: View) => {
    setView(v); setMenuIdx(0); setScrollIdx(0);
    if (v === "model" || v === "provider") {
      bridge.current?.send("fetch_models", {});
    }
  }, []);

  const onMenuSelect = useCallback((idx: number) => {
    const item = menuItems[idx];
    if (!item) return;
    if (item.disabled || item.value === "") {
      // Guidance/placeholder rows (e.g. "run /provider first") are not selectable.
      if (view === "model" || view === "provider") setView("chat");
      return;
    }
    if (view === "sessions") {
      if (item.value) bridge.current?.send("resume_session", {sessionId: item.value});
      setView("chat");
    } else if (view === "provider") {
      const pr = providers.find((p) => p.id === item.value);
      if (pr && !pr.configured) {
        // Not connected yet: walk the user through entering an API key. After
        // the key is saved the backend fetches models and we open /model.
        setConfigApiKeyTemp("");
        setBuffer(EMPTY);
        setConfigTarget({provider: item.value, step: "apiKey"});
        setView("chat");
        return;
      }
      // Already connected: let the user keep the saved key (→ models) or
      // re-enter it, instead of silently jumping past key entry.
      setProviderActionTarget(item.value);
      setMenuIdx(0);
      setView("providerAction");
    } else if (view === "providerAction") {
      const provider = providerActionTarget;
      if (item.value === "rekey") {
        setConfigApiKeyTemp("");
        setBuffer(EMPTY);
        setConfigTarget({provider, step: "apiKey"});
        setView("chat");
        return;
      }
      // "Use saved key" → make it active and open the model picker.
      const pr = providers.find((p) => p.id === provider);
      const model = pr?.models?.[0] || session?.model || "";
      bridge.current?.send("model_change", {provider, model});
      setMenuIdx(0);
      openView("model");
    } else if (view === "model") {
      // Item value is "<provider>::<model>"; fall back to the active provider
      // for legacy/fallback items that carry only a bare model id.
      const sep = item.value.indexOf("::");
      const prov = sep >= 0 ? item.value.slice(0, sep) : (session?.provider || "");
      const mdl = sep >= 0 ? item.value.slice(sep + 2) : item.value;
      bridge.current?.send("model_change", {provider: prov, model: mdl});
      setView("chat");
    } else if (view === "effort") {
      bridge.current?.send("reasoning_effort_change", {effort: item.value});
      setView("chat");
    } else if (view === "config") {
      setConfigApiKeyTemp("");
      setBuffer(EMPTY);
      setConfigTarget({provider: item.value, step: "apiKey"});
      setView("chat");
    }
  }, [view, menuItems, providers, session, openView, providerActionTarget]);

  const handleConfigSubmit = useCallback(() => {
    const t = configTarget;
    if (!t) return;
    const text = buffer.text.trim();
    if (t.step === "apiKey") {
      if (!text) { setConfigTarget(null); setConfigApiKeyTemp(""); return; }
      setConfigApiKeyTemp(text);
      setConfigTarget({provider: t.provider, step: "models"});
      setBuffer(EMPTY);
    } else {
      // Models are optional here — leaving it blank lets the backend fetch the
      // provider's model list from its API after the key is saved.
      const models = text ? text.split(",").map((s) => s.trim()).filter(Boolean) : [];
      bridge.current?.send("provider_config", {provider: t.provider, apiKey: configApiKeyTemp, models});
      append({id: `${Date.now()}`, kind: "system", text: `Connected ${t.provider}. Fetching models…`});
      setConfigTarget(null);
      setConfigApiKeyTemp("");
      setBuffer(EMPTY);
      // Open the model picker, which sends fetch_models so the backend pulls
      // the provider's live model list. The user picks a model there (which
      // then sets it active) — avoid setting an empty active model here.
      openView("model");
    }
  }, [configTarget, buffer.text, configApiKeyTemp, append, openView]);

  const submitChat = useCallback((override?: string) => {
    const text = (override ?? buffer.text).trim();
    if (!text && pendingImagesRef.current.length === 0) return;

    // Save to history
    historyRef.current = [text, ...historyRef.current.filter((h) => h !== text)].slice(0, 200);
    historyIdxRef.current = -1;
    setBuffer(EMPTY);
    setScrollIdx(0);
    setShowWelcome(false);

    // Allow always: navigation/view commands — even while busy
    const alwaysAllowed = ["/exit", "/quit", "/help", "/?", "/cache", "/mode",
      "/model", "/models", "/provider", "/providers", "/effort", "/sessions", "/resume", "/new",
      "/config", "/yolo", "/context", "/compact_size"];

    if (busyRef.current && !alwaysAllowed.includes(text)) {
      // Queue the message — will be sent when agent finishes
      setQueuedMessage(text);
      setStatusMsg("» Message queued — will send when ready. Esc to cancel.");
      return;
    }

    if (text === "/exit" || text === "/quit") {
      gracefulQuit();
      return;
    }
    if (text === "/sessions" || text === "/resume") { openView("sessions"); bridge.current?.send("request_sessions", {scope: sessionsScope}); return; }
    if (text === "/new") { resetTranscript([]); setSelectedId(null); setShowWelcome(true); bridge.current?.send("create_session"); return; }
    if (text === "/init") { bridge.current?.send("slash_command", {command: "/init"}); return; }
    if (text === "/compact") { bridge.current?.send("compact", {}); return; }
    if (text.startsWith("/compact ")) { bridge.current?.send("compact", {focus: text.slice("/compact ".length).trim()}); return; }
    if (text === "/compact_size") { bridge.current?.send("compact_size", {}); return; }
    if (text.startsWith("/compact_size ")) { bridge.current?.send("compact_size", {value: text.slice("/compact_size ".length).trim()}); return; }
    if (text === "/context") { bridge.current?.send("context_info", {}); return; }
    if (text === "/model" || text === "/models") { openView("model"); return; }
    if (text === "/provider" || text === "/providers") { openView("provider"); return; }
    if (text === "/effort") { openView("effort"); return; }
    if (text === "/config") { openView("config"); return; }
    if (text === "/cache") { openView("cache"); return; }
    if (text === "/help" || text === "/?") { openView("help"); return; }
    if (text === "/mode") {
      const newMode = session?.mode === "plan" ? "build" : "plan";
      bridge.current?.send("mode_change", {mode: newMode});
      setSession((s) => s ? {...s, mode: newMode} : s);
      append({id: `${Date.now()}`, kind: "system", text: `Switched to ${newMode} mode`});
      return;
    }
    if (text === "/yolo") {
      if (yoloMode) {
        setYoloMode(false);
        append({id: `${Date.now()}`, kind: "system", text: "YOLO mode disabled."});
      } else {
        setShowYoloTOS(true);
      }
      return;
    }

    const imgs = pendingImagesRef.current;
    append({id: `${Date.now()}`, kind: "user", text: text || `[${imgs.length} image${imgs.length === 1 ? "" : "s"} attached]`});
    setBusy(true);
    setStatusText("Working");
    bridge.current?.send("user_input", {text, imageUrls: imgs});
    if (imgs.length) setPendingImages([]);
  }, [buffer.text, exit, openView, sessionsScope, append]);

  // Stable refs for callbacks used in useInput to avoid re-subscribing stdin
  const submitChatRef = useRef(submitChat);
  submitChatRef.current = submitChat;
  const appendRef = useRef(append);
  appendRef.current = append;
  const handleConfigSubmitRef = useRef(handleConfigSubmit);
  handleConfigSubmitRef.current = handleConfigSubmit;
  const onMenuSelectRef = useRef(onMenuSelect);
  onMenuSelectRef.current = onMenuSelect;

  // Single source of truth for the chat-prompt Enter key, shared by both the
  // useInput and the useStdin (raw) handlers so they can never disagree (e.g.
  // one submitting the raw "/" while the other tries to run the menu command).
  // Both Ink's useInput and our raw stdin listener receive the same keypress,
  // so guard against a duplicate Enter within a short window to avoid
  // submitting the command twice.
  // Both Ink's useInput and our raw stdin listener receive the same physical
  // Enter keypress. Without a shared guard, one Enter can trigger two actions
  // (e.g. run "/provider" AND then immediately auto-select the first provider,
  // skipping the provider list). enterGuard() returns true when this Enter is a
  // duplicate within the window and should be ignored. Every Enter path —
  // chat submit and menu select — must funnel through it so a single keypress
  // only ever performs one action.
  const lastEnterRef = useRef(0);
  const enterGuardRef = useRef<() => boolean>(() => false);
  enterGuardRef.current = () => {
    const now = Date.now();
    if (now - lastEnterRef.current < 60) return true;
    lastEnterRef.current = now;
    return false;
  };
  const handleChatEnter = useCallback(() => {
    if (enterGuardRef.current()) return;
    if (showSlashMenu && slashMatches.length > 0) {
      const cmd = slashMatches[slashMenuIdx];
      if (cmd) {
        setBuffer(EMPTY);
        setSlashMenuIdx(0);
        submitChatRef.current(cmd.label);
        return;
      }
    }
    submitChatRef.current();
  }, [showSlashMenu, slashMatches, slashMenuIdx]);
  const handleChatEnterRef = useRef(handleChatEnter);
  handleChatEnterRef.current = handleChatEnter;

  // ═══════════════════════════════════════════════════════════════════════
  // Input handling: useInput for standard keys + useStdin for extended keys
  // ═══════════════════════════════════════════════════════════════════════

  // ── Tier 1: useInput (arrows, backspace, delete, enter, escape, tab,
  //    pageup/pagedn, printable chars, Ctrl/Shift/Meta modifiers) ──

  const gracefulQuit = useCallback(() => {
    bridge.current?.send("quit", {});
    const s = session;
    if (s) {
      const cachePct = s.cacheHitRate != null ? `${Math.round(s.cacheHitRate * 100)}%` : "-";
      // Build the summary now but defer printing until after Ink restores the
      // main screen (see render()), so it is not wiped with the alt-screen.
      pendingSummary =
        "\n" +
        chalk.rgb(0xff, 0x8c, 0x1a)("─".repeat(40)) + "\n" +
        chalk.rgb(0xff, 0x8c, 0x1a).bold(" TigerLiteCode Session Summary") + "\n\n" +
        `  Session:  ${s.id ?? "(none)"}\n` +
        `  Title:    ${s.title ?? "-"}\n` +
        `  Model:    ${s.provider ?? "-"}/${s.model ?? "-"}\n\n` +
        `  Tokens in:   ${fmtNum(s.tokensIn)}\n` +
        `  Tokens out:  ${fmtNum(s.tokensOut)}\n` +
        `  Cache hits:  ${fmtNum(s.cacheHitTokens)}\n` +
        `  Cache rate:  ${cachePct}\n\n` +
        `  Requests:    ${fmtNum(s.requests)}\n` +
        `  Cost (USD):  $${(s.cost ?? 0).toFixed(4)}\n\n` +
        chalk.dim("  Thanks for using TigerLiteCode!\n") +
        "\n";
    }
    exit();
  }, [session, exit]);

  const gracefulQuitRef = useRef(gracefulQuit);
  gracefulQuitRef.current = gracefulQuit;

  useInput((ch, key) => {
    // Globals
    if (key.ctrl && ch === "q") { gracefulQuitRef.current(); return; }
    if (key.ctrl && ch === "c") {
      bridge.current?.send("interrupt");
      appendRef.current({id: `${Date.now()}`, kind: "system", text: "Interrupted. Press Ctrl+Q to quit."});
      return;
    }

    // Permission prompt
    if (permissionId) {
      const c = ch.toLowerCase();
      if (c === "y" || c === "a" || c === "n") {
        bridge.current?.send("permission_response", {toolCallId: permissionId, approved: c !== "n", always: c === "a"});
        setPermissionId(null);
      }
      return;
    }

    // Cache / Help views
    if (view === "cache" || view === "help") {
      if (key.escape || key.return) setView("chat");
      else if (key.pageUp) setScrollIdx((n) => n + 8);
      else if (key.pageDown) setScrollIdx((n) => Math.max(0, n - 8));
      return;
    }

    // Menu views
    if (view !== "chat") {
      if (key.escape) {
        // From the connected-provider action menu, Esc returns to the provider
        // list rather than all the way back to chat.
        if (view === "providerAction") { setMenuIdx(0); setView("provider"); return; }
        setView("chat");
        return;
      }
      if (view === "sessions" && key.tab) {
        const next = sessionsScope === "project" ? "all" : "project";
        setSessionsScope(next);
        bridge.current?.send("request_sessions", {scope: next});
        setMenuIdx(0);
        return;
      }
      if (key.upArrow) { setMenuIdx((n) => Math.max(0, n - 1)); return; }
      if (key.downArrow) { if (menuItems.length > 0) setMenuIdx((n) => Math.min(menuItems.length - 1, n + 1)); return; }
      // Share the Enter de-dup with chat submit so the same physical Enter that
      // opened this menu (e.g. via "/provider") cannot immediately auto-select
      // the first row and skip the provider list.
      if (key.return) { if (enterGuardRef.current()) return; onMenuSelectRef.current(menuIdx); return; }
      return;
    }

    // Config prompt mode
    if (configTarget) {
      if (key.escape) { setConfigTarget(null); setConfigApiKeyTemp(""); setBuffer(EMPTY); return; }
      if (key.return) { handleConfigSubmitRef.current(); return; }
      if (key.backspace) { setBuffer(bufBackspace); return; }
      if (key.delete) { setBuffer(bufDelete); return; }
      if (key.leftArrow && key.ctrl) { setBuffer(bufMoveWordLeft); return; }
      if (key.rightArrow && key.ctrl) { setBuffer(bufMoveWordRight); return; }
      if (key.leftArrow) { setBuffer(bufMoveLeft); return; }
      if (key.rightArrow) { setBuffer(bufMoveRight); return; }
      if (key.upArrow) { setBuffer(bufMoveUp); return; }
      if (key.downArrow) { setBuffer(bufMoveDown); return; }
      if (key.ctrl && ch === "a") { setBuffer(bufMoveLineStart); return; }
      if (key.ctrl && ch === "e") { setBuffer(bufMoveLineEnd); return; }
      if (key.ctrl && ch === "k") { setBuffer(bufKillToEnd); return; }
      if (key.ctrl && ch === "d") { setBuffer(bufDelete); return; }
      if (ch && !key.ctrl && !key.meta && !key.tab) { setBuffer((s) => bufInsert(s, ch)); return; }
      return;
    }

    // ── Chat mode ──

    // Ctrl+Y: toggle YOLO mode
    if (key.ctrl && ch === "y") {
      if (yoloMode) {
        setYoloMode(false);
        setStatusMsg("YOLO mode disabled.");
      } else if (showYoloTOS) {
        setShowYoloTOS(false);
        forceRedraw();
        setYoloMode(true);
        setStatusMsg("▲ YOLO mode enabled — bash/delete still require approval.");
      } else {
        setShowYoloTOS(true);
      }
      return;
    }
    // Handle TOS agreement while TOS is shown
    if (showYoloTOS) {
      const c = ch.toLowerCase();
      if (c === "y") {
        setShowYoloTOS(false);
        forceRedraw();
        setYoloMode(true);
        setStatusMsg("▲ YOLO mode enabled — bash/delete still require approval.");
      } else if (c === "n" || key.escape) {
        setShowYoloTOS(false);
        forceRedraw();
        setStatusMsg("YOLO mode cancelled.");
      }
      return;
    }

    // Ctrl+V: paste an image from the clipboard (vision models only — the
    // backend drops it with a notice for text-only models). Text paste arrives
    // via the terminal's bracketed-paste protocol (handled in the raw stdin
    // listener), which is also what Ctrl+Shift+V triggers in most terminals.
    if (key.ctrl && (ch === "v" || ch === "V")) {
      setStatusMsg("Reading clipboard…");
      readClipboardImageAsync().then((img) => {
        if (img) {
          setPendingImages((p) => [...p, img.dataUrl]);
          setStatusMsg("Attached image from clipboard");
        } else {
          setStatusMsg("No image found in clipboard");
        }
      }).catch(() => setStatusMsg("Failed to read clipboard"));
      return;
    }
    // Ctrl+X: clear any attached images.
    if (key.ctrl && (ch === "x" || ch === "X")) {
      if (pendingImagesRef.current.length) {
        setPendingImages([]);
        setStatusMsg("Cleared attached images");
      }
      return;
    }

    if (key.escape) {
      if (queuedMessage) {
        setQueuedMessage(null);
        setStatusMsg("Queued message cancelled.");
        return;
      }
      if (showSlashMenu) {
        // Just clear the slash token by adding a space, or clear buffer
        setBuffer((s) => {
          const spaceIdx = s.text.indexOf(" ");
          if (spaceIdx > 0) return {text: s.text.slice(0, spaceIdx) + " " + s.text.slice(spaceIdx + 1), cursor: s.cursor};
          return EMPTY;
        });
        return;
      }
      bridge.current?.send("interrupt");
      return;
    }

    // Slash menu: Enter to select, up/down to navigate
    if (showSlashMenu && slashMatches.length > 0) {
      if (key.return) {
        handleChatEnterRef.current();
        return;
      }
      if (key.upArrow) { setSlashMenuIdx((n) => Math.max(0, n - 1)); return; }
      if (key.downArrow) { setSlashMenuIdx((n) => Math.min(slashMatches.length - 1, n + 1)); return; }
      if (key.tab) {
        setSlashMenuIdx((n) => (n + 1) % slashMatches.length);
        return;
      }
    } else if (showSlashMenu) {
      // Menu is open but no command matches the query: still swallow arrow
      // keys so they don't leak into history navigation and clobber the query.
      if (key.upArrow || key.downArrow) return;
    }

    // Reset slash menu index when slash token changes
    // (handled via useMemo dependency above)
    if (!showSlashMenu && slashMenuIdx !== 0) {
      // will be reset on next render
    }

    // History navigation
    if (key.upArrow && (!buffer.text || historyIdxRef.current >= 0)) {
      const hist = historyRef.current;
      if (hist.length > 0) {
        const nextIdx = Math.min(historyIdxRef.current + 1, hist.length - 1);
        historyIdxRef.current = nextIdx;
        const txt = hist[nextIdx];
        setBuffer({text: txt, cursor: txt.length});
      }
      return;
    }
    if (key.downArrow && historyIdxRef.current >= 0) {
      const hist = historyRef.current;
      const nextIdx = historyIdxRef.current - 1;
      if (nextIdx < 0) { historyIdxRef.current = -1; setBuffer(EMPTY); }
      else { historyIdxRef.current = nextIdx; setBuffer({text: hist[nextIdx], cursor: hist[nextIdx].length}); }
      return;
    }

    if (historyIdxRef.current >= 0 && (ch || key.backspace || key.delete || key.leftArrow || key.rightArrow)) {
      historyIdxRef.current = -1;
    }

    // Scroll
    const visibleCount = computeVisibleCount(height);
    const maxOff = Math.max(0, items.length - visibleCount);
    if (key.pageUp) { setScrollIdx((n) => Math.min(maxOff, n + 8)); return; }
    if (key.pageDown) { setScrollIdx((n) => Math.max(0, n - 8)); return; }

    // Focus expansion
    if (key.tab && expandableIds.length > 0) {
      setSelectedId((cur) => {
        const i = cur ? expandableIds.indexOf(cur) : -1;
        const dir = key.shift ? -1 : 1;
        return expandableIds[(i + dir + expandableIds.length) % expandableIds.length];
      });
      return;
    }

    // Cursor movement
    if (key.leftArrow && key.ctrl) { setBuffer(bufMoveWordLeft); return; }
    if (key.rightArrow && key.ctrl) { setBuffer(bufMoveWordRight); return; }
    if (key.leftArrow && key.meta) { setBuffer(bufMoveWordLeft); return; }
    if (key.rightArrow && key.meta) { setBuffer(bufMoveWordRight); return; }
    if (key.leftArrow) { setBuffer(bufMoveLeft); return; }
    if (key.rightArrow) { setBuffer(bufMoveRight); return; }
    if (key.upArrow) { setBuffer(bufMoveUp); return; }
    if (key.downArrow) { setBuffer(bufMoveDown); return; }

    // Edit keys
    if (key.return) { handleChatEnterRef.current(); return; }
    if (key.backspace && key.ctrl) { setBuffer(bufDeleteWordBefore); return; }
    if (key.backspace && key.meta) { setBuffer(bufDeleteWordBefore); return; }
    if (key.backspace) { setBuffer(bufBackspace); return; }
    if (key.delete) { setBuffer(bufDelete); return; }

    // Ctrl combos
    if (key.ctrl && ch === "a") { setBuffer(bufMoveLineStart); return; }
    if (key.ctrl && ch === "e") { setBuffer(bufMoveLineEnd); return; }
    if (key.ctrl && ch === "k") { setBuffer(bufKillToEnd); return; }
    if (key.ctrl && ch === "d") { setBuffer(bufDelete); return; }

    // Printable char
    if (ch && !key.ctrl && !key.meta && !key.tab) {
      setBuffer((s) => bufInsert(s, ch));
      return;
    }
  });

  // ── Tier 2: useStdin ONLY for keys useInput doesn't cover:
  //    Home, End, Ctrl+Backspace, Ctrl+Delete, Alt+Backspace, paste ──

  const {stdin} = useStdin();
  useEffect(() => {
    if (!stdin) return;
    const handler = (raw: string) => {
      // Filter focus reporting and other terminal control sequences
      if (raw === "\x1b[I" || raw === "\x1b[O") return; // focus in/out
      if (raw.startsWith("\x1b[?") || raw.startsWith("\x1b[>") || raw.startsWith("\x1b[=")) return; // CSI sequences
      if (raw === "\x1b" && pendingPasteRef.current === null) return; // lone escape

      // Paste tracking. Terminals may deliver the bracketed-paste markers
      // ("\x1b[200~" start, "\x1b[201~" end) glued to the pasted body inside a
      // single data chunk (e.g. "\x1b[200~cd foo\x1b[201~"), so we must scan for
      // the markers anywhere in `raw` rather than matching the whole chunk with
      // ===. Otherwise the marker tail ("[200~") leaks into the buffer.
      const START = "\x1b[200~";
      const END = "\x1b[201~";
      if (raw.includes(START) || raw.includes(END) || pendingPasteRef.current !== null) {
        let rest = raw;
        const commit = () => {
          const txt = pendingPasteRef.current ?? "";
          pendingPasteRef.current = null;
          if (txt && !busy && view === "chat" && !permissionId && !configTarget) {
            setBuffer((s) => bufInsert(s, txt));
            setStatusMsg(`Pasted ${txt.length} characters`);
          }
        };
        while (rest.length > 0) {
          if (pendingPasteRef.current === null) {
            const s = rest.indexOf(START);
            if (s === -1) {
              // No start marker; nothing to accumulate. Any stray END marker is
              // dropped. Remaining text before a start was already consumed by
              // the normal key path on its own chunk, so ignore here.
              rest = "";
              break;
            }
            // Begin accumulating after the start marker.
            pendingPasteRef.current = "";
            rest = rest.slice(s + START.length);
          } else {
            const e = rest.indexOf(END);
            if (e === -1) {
              // No end yet: accumulate the whole remainder and wait for more.
              pendingPasteRef.current += rest;
              rest = "";
              break;
            }
            // Found the end: accumulate up to it, commit, continue scanning.
            pendingPasteRef.current += rest.slice(0, e);
            rest = rest.slice(e + END.length);
            commit();
          }
        }
        return;
      }

      if (permissionId) return;
      if (view !== "chat" && view !== "cache" && view !== "help") return;
      // NOTE: we intentionally do NOT drop keys while busy. The user must be
      // able to type AND edit (backspace, navigation) in the prompt while the
      // agent runs, so they can compose/queue the next message. Only submitting
      // (Enter) is deferred — handleChatEnter queues it while busy.

      // Backspace (safety net — some terminals/Ink versions miss this)
      if (raw === "\x7f" || raw === "\b") {
        setBuffer(bufBackspace);
        return;
      }
      // Enter (safety net — ensure it always works)
      if (raw === "\r" || raw === "\n") {
        if (view === "chat" && !configTarget && !permissionId && !showYoloTOS) {
          handleChatEnterRef.current();
        }
        return;
      }
      // Home / End
      if (raw === "\x1b[H" || raw === "\x1b[1~" || raw === "\x1b[7~" || raw === "\x1bOH") {
        if (view === "cache" || view === "help") setScrollIdx(0);
        else setBuffer(bufMoveLineStart);
        return;
      }
      if (raw === "\x1b[F" || raw === "\x1b[4~" || raw === "\x1b[8~" || raw === "\x1bOF") {
        setBuffer(bufMoveLineEnd);
        return;
      }
      // Ctrl+Delete (delete word after cursor)
      if (raw === "\x1b[3;5~") { setBuffer(bufDeleteWordAfter); return; }
      // Alt+Backspace or Ctrl+W (delete word before cursor)
      if (raw === "\x1b\x7f" || raw === "\x1b\b" || raw === "\x17") { setBuffer(bufDeleteWordBefore); return; }
    };
    stdin.on("data", handler);
    // Safety: reset stuck paste state after 5 seconds
    const pasteTimer = setInterval(() => {
      if (pendingPasteRef.current !== null) {
        pendingPasteRef.current = null;
      }
    }, 5000);
    return () => {
      clearInterval(pasteTimer);
      stdin.off("data", handler);
    };
  }, [stdin, busy, view, permissionId, configTarget, showYoloTOS]);

  // ---- Render helpers ----

  const configPrompt = configTarget
    ? configTarget.step === "apiKey"
      ? `API key for ${configTarget.provider}: `
      : `Models for ${configTarget.provider} (comma-separated, Enter to skip): `
    : "";

  const cacheLines = useMemo(() => {
    const s = session;
    const cachePct = s?.cacheHitRate != null ? `${Math.round(s.cacheHitRate * 100)}%` : "-";
    const lines: string[] = [];
    lines.push(`Session:   ${s?.id ?? "(none)"}  ${s?.title ?? ""}`);
    lines.push(`Project:   ${projectPath || "-"}`);
    lines.push(`Provider:  ${s?.provider ?? "-"} / ${s?.model ?? "-"}`);
    lines.push(`Mode:      ${s?.mode ?? "build"}   Effort: ${s?.reasoningEffort ?? "high"}`);
    lines.push("");
    lines.push(`Tokens in:     ${fmtNum(s?.tokensIn)}`);
    lines.push(`Tokens out:    ${fmtNum(s?.tokensOut)}`);
    lines.push(`Cache hit:     ${fmtNum(s?.cacheHitTokens)}`);
    lines.push(`Cache miss:    ${fmtNum(s?.cacheMissTokens)}`);
    lines.push(`Cache rate:    ${cachePct}`);
    lines.push(`Requests:      ${fmtNum(s?.requests)}`);
    lines.push(`Cost (USD):    ${(s?.cost ?? 0).toFixed(4)}`);
    lines.push(`Updated:       ${String(s?.updatedAt ?? "-").slice(0, 19)}`);
    lines.push("");
    lines.push("Providers:");
    if (providers.length === 0) lines.push("  (no provider data from backend)");
    for (const pr of providers) {
      lines.push(`  ${pr.id}${pr.configured ? "  ✓ configured" : "  ✗ not configured"}${pr.models.length ? `  (${pr.models.length} models)` : ""}`);
    }
    return lines;
  }, [session, projectPath, providers]);

  const helpLines = useMemo(() => [
    "Commands:",
    "  /sessions, /resume   List & resume sessions. Tab toggles all-projects.",
    "  /new                 Start a fresh session",
    "  /init                Create AGENTS.md in the project",
    "  /compact [focus]     Compact conversation history",
    "  /compact_size <n>    Set auto-compact token threshold (e.g. 500k, 1m)",
    "  /context             Show context/token usage",
    "  /model               Choose model",
    "  /provider            Choose provider",
    "  /effort              Change reasoning effort: low | medium | high | max",
    "  /config              Configure a provider API key + models",
    "  /cache               Show session cache/token/cost",
    "  /mode                Toggle build/plan mode",
    "  /yolo                Toggle YOLO mode (auto-approve tools)",
    "  /help                This help",
    "  /exit, /quit         Quit",
    "",
    "Editing:",
    "  Left/Right arrows    Move cursor",
    "  Ctrl+Left/Right      Move by word",
    "  Home/End             Line start/end",
    "  Ctrl+A / Ctrl+E      Line start/end",
    "  Ctrl+Backspace       Delete word before",
    "  Ctrl+K               Kill to end of line",
    "  Ctrl+D               Delete forward (or exit if empty)",
    "  Ctrl+Y               Toggle YOLO mode",
    "  Up/Down arrows       Browse input history",
    "",
    "Keys:",
    "  Enter          Send message",
    "  PgUp/PgDn      Scroll transcript",
    "  Esc            Interrupt current turn (or close a panel)",
    "  Ctrl+C         Interrupt current agent turn",
    "  Ctrl+Q         Quit",
    "  Tab            Focus next expandable item",
    "  y / a / n      Approve once / always / deny a permission",
  ], []);

  // Split the transcript into a finalized "committed" prefix and a live tail.
  // Committed items are printed ONCE via <Static> — they flow into the
  // terminal's native scrollback (so earlier turns are scrollable) and are
  // never re-rendered, which eliminates the flicker/reordering/missing-label
  // problems of re-painting the whole list every frame. Only the streaming
  // tail is re-rendered each tick.
  //
  // Items only ever transition streaming:true → false (never back) and their
  // order is stable, so the committed prefix grows monotonically — exactly the
  // contract <Static> requires.
  // Split the transcript into a committed prefix (flushed once into <Static> /
  // native scrollback) and a live tail (re-rendered every animation tick).
  // Items only ever go streaming:true → false and are appended in chronological
  // order, so the prefix before the first streaming item is finalized and grows
  // monotonically — exactly <Static>'s contract. Everything from the first
  // streaming item onward is the live tail.
  let firstLiveIdx = items.length;
  for (let i = 0; i < items.length; i++) {
    if (items[i].streaming) { firstLiveIdx = i; break; }
  }
  const committedItems = staticSuspended ? [] : items.slice(0, firstLiveIdx);
  const liveItems = staticSuspended ? [] : items.slice(firstLiveIdx);

  return (
    <Box flexDirection="column" width={width}>
      {/* Finalized transcript — MUST be the first child so Ink commits it once
          into native scrollback ABOVE the persistent dynamic frame below.
          Placing any re-rendering node (e.g. the status bar) before <Static>
          makes Ink reprint that frame on every flush, stacking duplicate
          status bars / prompts in the scrollback. */}
      <Static items={committedItems}>
        {(item) => (
          <MessageItem
            key={item.id}
            item={item}
            width={Math.max(20, width - 2)}
            focused={false}
            expandedThinkingId={expandedThinkingId}
            tick={0}
          />
        )}
      </Static>
      {/* Live streaming tail — content height only. The status bar is a single
          truncated line and finalized output lives in scrollback above, so the
          dynamic frame stays short and Ink redraws it in place rather than
          stacking copies. */}
      <Box flexDirection="column" width={width}>
        {view === "chat" ? (
          showWelcome && items.length === 0 ? (
            <WelcomeScreen projectPath={projectPath} session={session} providers={providers} width={width} />
          ) : (
            <Box flexDirection="column" width={width}>
              {liveItems.map((item) => (
                <MessageItem
                  key={item.id}
                  item={item}
                  width={Math.max(20, width - 2)}
                  focused={item.id === selectedId}
                  expandedThinkingId={expandedThinkingId}
                  tick={animTick}
                />
              ))}
            </Box>
          )
        ) : null}
      </Box>
      {view === "sessions" ? (
        <MenuView
          title="Resume Session"
          hint={`scope: ${sessionsScope === "project" ? "current project" : "all projects"} · ↑/↓ move · Enter resume · Tab toggle scope · Esc back`}
          items={menuItems} idx={menuIdx}
          footer={sessionsScope === "project" ? `Project: ${projectPath || "-"}` : "Showing sessions from all projects"}
        />
      ) : view === "provider" ? (
        <MenuView title="Choose Provider" hint="↑/↓ move · Enter select · Esc back" items={menuItems} idx={menuIdx} />
      ) : view === "providerAction" ? (
        <MenuView title={`${providerActionTarget} — connected`} hint="↑/↓ move · Enter select · Esc back" items={menuItems} idx={menuIdx} />
      ) : view === "model" ? (
        <MenuView title="Available Models" hint="Select a model · Esc back" items={menuItems} idx={menuIdx} />
      ) : view === "effort" ? (
        <MenuView title="Reasoning Effort" hint="↑/↓ move · Enter select · Esc back" items={menuItems} idx={menuIdx} />
      ) : view === "config" ? (
        <MenuView title="Configure Provider" hint="Pick a provider to set its API key · Esc back" items={menuItems} idx={menuIdx} />
      ) : view === "cache" ? (
        <InfoView title="Cache & Usage" lines={cacheLines} width={width} />
      ) : view === "help" ? (
        <InfoView title="Help" lines={helpLines} width={width} />
      ) : (
        /* Bottom frame: status bar + transient prompts + input, pinned together
           at the very bottom. The live transcript is rendered above this block,
           so streaming/thinking never appears between the status bar and the
           input. */
        <>
          <StatusBar busy={busy} statusText={statusText} session={session} width={width} yoloMode={yoloMode} paused={animPaused} />
          {permissionId ? (
            <Box paddingX={0} width={width} flexShrink={0}>
              <Text color="yellow" bold>{SYM.approval} approval  </Text>
              <Text>Y approve once · A always · N deny</Text>
            </Box>
          ) : null}
          {showSlashMenu ? (
            <SlashCommandMenu query={slashToken ?? ""} idx={slashMenuIdx} width={width} />
          ) : null}
          {pendingImages.length > 0 ? (
            <Box width={width} flexShrink={0}>
              <Text color={TC.accent}>{`[${pendingImages.length} image${pendingImages.length === 1 ? "" : "s"} attached — Ctrl+X to clear]`}</Text>
            </Box>
          ) : null}
          {statusMsg ? (
            <Box width={width} flexShrink={0}>
              <Text color="gray">{statusMsg}</Text>
            </Box>
          ) : null}
          {showYoloTOS ? (
            <Box flexDirection="column" borderStyle="double" borderColor={TC.yolo} paddingX={2} width={width} flexShrink={0}>
              {YOLO_TOS_LINES.map((line, i) => (
                <Text key={i} color={line.color as any} bold={line.bold} backgroundColor={i === 0 ? TC.yolo : undefined}>
                  {i === 0 ? "  " + line.text + "  " : line.text || " "}
                </Text>
              ))}
            </Box>
          ) : null}
          {queuedMessage ? (
            <Box paddingX={0} width={width} flexShrink={0}>
              <Text color="yellow">» Queued: </Text>
              <Text dimColor>{queuedMessage.slice(0, 60)}{queuedMessage.length > 60 ? "…" : ""}</Text>
              <Text color="gray"> — Esc to cancel</Text>
            </Box>
          ) : null}
          <Box paddingX={0} width={width} flexShrink={0}>
            {permissionId ? (
              <Text color="yellow"><Text bold>{SYM.focus} </Text>y approve · a always · n deny</Text>
            ) : (
              <PromptLine buffer={buffer} width={Math.max(10, width - 4)} placeholder="Type a message…" configPrompt={configPrompt} />
            )}
          </Box>
        </>
      )}
      <Box width={width} flexShrink={0}>
        <Text color="gray">←→ move cursor · Ctrl+←→ word · Home/End · ↑↓ history · PgUp/PgDn scroll · Esc interrupt · Ctrl+V image · Ctrl+Q quit · /help</Text>
      </Box>
    </Box>
  );
}

const app = render(<App />);
// The Ink cleanup effect restores the main screen buffer on unmount. Only after
// that finishes do we print the session summary, so it lands on the normal
// terminal (visible at the shell prompt) instead of the cleared alt-screen.
app.waitUntilExit().then(() => {
  if (pendingSummary) {
    process.stdout.write(pendingSummary);
    pendingSummary = "";
  }
}).catch(() => { /* ignore */ });

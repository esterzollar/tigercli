// Read an image from the OS clipboard and return it as a base64 data URL.
//
// Adapted from the deepcode-cli clipboard module: shell out to the platform's
// clipboard tool, capture raw PNG bytes, and encode them as
// "data:image/png;base64,...". Returns null when there is no image in the
// clipboard (or no supported tool is installed).

import {spawnSync} from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export type ClipboardImage = {
  dataUrl: string; // "data:image/png;base64,..."
  mimeType: string; // e.g. "image/png"
};

const PNG_MIME = "image/png";

const IMAGE_MIME_BY_EXT = new Map<string, string>([
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".gif", "image/gif"],
  [".webp", "image/webp"],
]);

function bufferToDataUrl(buffer: Buffer, mimeType: string): string {
  return `data:${mimeType};base64,${buffer.toString("base64")}`;
}

// Run a command and return its stdout as a Buffer, or null on failure / empty.
function tryRun(cmd: string, args: string[]): Buffer | null {
  try {
    const res = spawnSync(cmd, args, {maxBuffer: 64 * 1024 * 1024});
    if (res.status === 0 && res.stdout && res.stdout.length > 0) {
      return res.stdout as Buffer;
    }
    return null;
  } catch {
    return null;
  }
}

function tryRunStatus(cmd: string, args: string[]): boolean {
  try {
    const res = spawnSync(cmd, args, {maxBuffer: 64 * 1024 * 1024});
    return res.status === 0;
  } catch {
    return false;
  }
}

function readImageFile(filePath: string): ClipboardImage | null {
  try {
    const ext = path.extname(filePath).toLowerCase();
    const mime = IMAGE_MIME_BY_EXT.get(ext);
    if (!mime) return null;
    const buf = fs.readFileSync(filePath);
    if (!buf || buf.length === 0) return null;
    return {dataUrl: bufferToDataUrl(buf, mime), mimeType: mime};
  } catch {
    return null;
  }
}

function readLinuxClipboardImage(): ClipboardImage | null {
  // X11 (xclip)
  const xclip = tryRun("xclip", ["-selection", "clipboard", "-t", "image/png", "-o"]);
  if (xclip && xclip.length > 0) {
    return {dataUrl: bufferToDataUrl(xclip, PNG_MIME), mimeType: PNG_MIME};
  }
  // Wayland (wl-paste)
  const wlPaste = tryRun("wl-paste", ["--type", "image/png"]);
  if (wlPaste && wlPaste.length > 0) {
    return {dataUrl: bufferToDataUrl(wlPaste, PNG_MIME), mimeType: PNG_MIME};
  }
  return null;
}

function readMacClipboardImage(): ClipboardImage | null {
  // pngpaste is the fast path when installed.
  const pngpaste = tryRun("pngpaste", ["-"]);
  if (pngpaste && pngpaste.length > 0) {
    return {dataUrl: bufferToDataUrl(pngpaste, PNG_MIME), mimeType: PNG_MIME};
  }
  // Fallback: ask AppleScript to write the clipboard PNG to a temp file.
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "tigercli-clipboard-"));
  const screenshotPath = path.join(tempDir, "clipboard.png");
  try {
    const saved = tryRunStatus("osascript", [
      "-e", "set png_data to (the clipboard as «class PNGf»)",
      "-e", `set fp to open for access POSIX file "${screenshotPath}" with write permission`,
      "-e", "write png_data to fp",
      "-e", "close access fp",
    ]);
    if (saved) {
      const image = readImageFile(screenshotPath);
      if (image) return image;
    }
    // Last resort: the clipboard may hold a file reference to an image.
    const fileUrl = tryRun("osascript", ["-e", "get POSIX path of (the clipboard as «class furl»)"]);
    const filePath = fileUrl?.toString("utf8").trim();
    if (filePath) return readImageFile(filePath);
    return null;
  } catch {
    return null;
  } finally {
    try {
      fs.rmSync(tempDir, {recursive: true, force: true});
    } catch {
      /* ignore cleanup errors */
    }
  }
}

function readWindowsClipboardImage(): ClipboardImage | null {
  const script =
    "Add-Type -AssemblyName System.Windows.Forms;" +
    "Add-Type -AssemblyName System.Drawing;" +
    "$img = [System.Windows.Forms.Clipboard]::GetImage();" +
    "if ($img) { $ms = New-Object System.IO.MemoryStream;" +
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);" +
    "[Console]::OpenStandardOutput().Write($ms.ToArray(), 0, $ms.Length); }";
  const out = tryRun("powershell", ["-NoProfile", "-Command", script]);
  if (out && out.length > 0) {
    return {dataUrl: bufferToDataUrl(out, PNG_MIME), mimeType: PNG_MIME};
  }
  return null;
}

export function readClipboardImage(): ClipboardImage | null {
  switch (process.platform) {
    case "darwin":
      return readMacClipboardImage();
    case "win32":
      return readWindowsClipboardImage();
    default:
      return readLinuxClipboardImage();
  }
}

// Async wrapper so reading the clipboard never blocks Ink's render loop.
export async function readClipboardImageAsync(): Promise<ClipboardImage | null> {
  return new Promise((resolve) => {
    setImmediate(() => {
      try {
        resolve(readClipboardImage());
      } catch {
        resolve(null);
      }
    });
  });
}

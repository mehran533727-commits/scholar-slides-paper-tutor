import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

function pathFor(platform) {
  return platform === "win32" ? path.win32 : path.posix;
}

export function isWindowsUncPath(candidate) {
  return typeof candidate === "string" && /^\\\\[^\\]+\\[^\\]+/.test(candidate);
}

export function pythonVenvPath(rootDir, platform = process.platform) {
  const paths = pathFor(platform);
  return platform === "win32"
    ? paths.join(rootDir, ".venv", "Scripts", "python.exe")
    : paths.join(rootDir, ".venv", "bin", "python");
}

export function resolvePythonCommand({
  rootDir,
  env = process.env,
  platform = process.platform,
  exists = fs.existsSync,
} = {}) {
  if (!rootDir) throw new Error("rootDir is required to resolve Python");
  if (env.SCHOLAR_SLIDES_PYTHON) {
    return { command: env.SCHOLAR_SLIDES_PYTHON, args: [], source: "environment" };
  }

  const venv = pythonVenvPath(rootDir, platform);
  if (exists(venv)) return { command: venv, args: [], source: "venv" };
  if (platform === "win32") return { command: "py", args: ["-3.11"], source: "windows-launcher" };
  // Do not silently select Ubuntu 22.04's system Python 3.10: the migration baseline is 3.11+.
  return { command: "python3.11", args: [], source: "path" };
}

export function resolveChromiumLaunchOptions({
  env = process.env,
  platform = process.platform,
  exists = fs.existsSync,
} = {}) {
  const explicit = env.SCHOLAR_SLIDES_CHROMIUM_EXECUTABLE;
  if (explicit) {
    if (!exists(explicit)) throw new Error(`SCHOLAR_SLIDES_CHROMIUM_EXECUTABLE does not exist: ${explicit}`);
    return { headless: true, executablePath: explicit };
  }
  if (platform !== "win32") return { headless: true };
  const candidates = [
    env.ProgramFiles && path.win32.join(env.ProgramFiles, "Google", "Chrome", "Application", "chrome.exe"),
    env["ProgramFiles(x86)"] && path.win32.join(env["ProgramFiles(x86)"], "Google", "Chrome", "Application", "chrome.exe"),
    env.LOCALAPPDATA && path.win32.join(env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe"),
    env.ProgramFiles && path.win32.join(env.ProgramFiles, "Microsoft", "Edge", "Application", "msedge.exe"),
    env["ProgramFiles(x86)"] && path.win32.join(env["ProgramFiles(x86)"], "Microsoft", "Edge", "Application", "msedge.exe"),
  ].filter(Boolean);
  const executablePath = candidates.find((candidate) => exists(candidate));
  if (!executablePath) throw new Error("No Windows Chrome or Edge executable is available for scholar-slides rendering");
  return { headless: true, executablePath };
}

export function isMain(moduleUrl, entryPath, { resolve = path.resolve, toFileUrl = pathToFileURL } = {}) {
  if (!entryPath) return false;
  try {
    const modulePath = fs.realpathSync(fileURLToPath(moduleUrl));
    const entryRealPath = fs.realpathSync(fileURLToPath(toFileUrl(resolve(entryPath)).href));
    return modulePath === entryRealPath;
  } catch {
    return false;
  }
}

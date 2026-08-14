import fs from "node:fs";
import path from "node:path";

function resourceError(detail) {
  return new Error(`resource URI ${detail}`);
}

export function normalizeResourceUri(value) {
  if (typeof value !== "string" || !value) throw resourceError("must be a non-empty string");
  if (path.posix.isAbsolute(value) || path.win32.isAbsolute(value)) {
    throw resourceError("must be relative, not an absolute, drive, or UNC path");
  }
  if (value.includes("\\")) throw resourceError("must use POSIX '/' separators, not backslashes");
  if (value === ".." || value.startsWith("../") || value.includes("/../")) {
    throw resourceError("must not contain parent traversal");
  }
  const normalized = path.posix.normalize(value);
  if (normalized === "." || normalized === ".." || normalized.startsWith("../")) {
    throw resourceError("must resolve below the resource root");
  }
  return normalized;
}

export function resolveResourcePath(rootDir, resourceUri, { mustExist = false } = {}) {
  const root = fs.realpathSync(rootDir);
  const relative = normalizeResourceUri(resourceUri);
  const candidate = path.resolve(root, relative);
  const relativeToRoot = path.relative(root, candidate);
  if (relativeToRoot === ".." || relativeToRoot.startsWith(`..${path.sep}`) || path.isAbsolute(relativeToRoot)) {
    throw resourceError("escapes allowed root");
  }
  if (!fs.existsSync(candidate)) {
    if (mustExist) throw resourceError(`does not exist: ${relative}`);
    return candidate;
  }
  const resolved = fs.realpathSync(candidate);
  const resolvedRelative = path.relative(root, resolved);
  if (resolvedRelative === ".." || resolvedRelative.startsWith(`..${path.sep}`) || path.isAbsolute(resolvedRelative)) {
    throw resourceError("escapes allowed root through a symlink");
  }
  return resolved;
}

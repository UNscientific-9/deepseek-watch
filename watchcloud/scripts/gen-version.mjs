// 部署前把「版本号 + git commit + 静态资源指纹」写进 src/version.js，供页面页脚和缓存版本使用。
// 版本号唯一来源是 package.json 的 version；这里只做传递，不判断该不该涨版本。
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");

const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const version = String(pkg.version || "0.0.0").trim();

// 优先直接读 .git 里的文件：不依赖子进程，受限环境里也能拿到真实 commit。
function commitFromGitFiles(startDir) {
  let dir = path.resolve(startDir);
  for (let depth = 0; depth < 6; depth += 1) {
    const gitPath = path.join(dir, ".git");
    if (fs.existsSync(gitPath)) {
      const stat = fs.statSync(gitPath);
      let gitDir = gitPath;
      if (!stat.isDirectory()) {
        // worktree / submodule：.git 是文本文件，内容形如 "gitdir: <path>"
        const text = fs.readFileSync(gitPath, "utf8").trim();
        const match = text.match(/^gitdir:\s*(.+)$/);
        if (!match) return "";
        gitDir = path.resolve(dir, match[1].trim());
      }
      const head = fs.readFileSync(path.join(gitDir, "HEAD"), "utf8").trim();
      if (!head.startsWith("ref:")) return head.slice(0, 7); // detached HEAD
      const ref = head.slice(4).trim();
      // worktree 的 refs 在 commondir 指向的主仓库里，HEAD 仍在 worktree 的 gitdir。
      const commonDirFile = path.join(gitDir, "commondir");
      if (fs.existsSync(commonDirFile)) {
        gitDir = path.resolve(gitDir, fs.readFileSync(commonDirFile, "utf8").trim());
      }
      const refFile = path.join(gitDir, ref);
      if (fs.existsSync(refFile)) return fs.readFileSync(refFile, "utf8").trim().slice(0, 7);
      const packedPath = path.join(gitDir, "packed-refs");
      if (fs.existsSync(packedPath)) {
        for (const line of fs.readFileSync(packedPath, "utf8").split("\n")) {
          if (line.startsWith("#") || !line.includes(" ")) continue;
          const [sha, name] = line.split(" ");
          if (name && name.trim() === ref) return sha.slice(0, 7);
        }
      }
      return "";
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return "";
}

let commit = commitFromGitFiles(root);
if (!commit) {
  // 兜底：正常机器上 git 可用时走这条
  try {
    commit = execFileSync("git", ["rev-parse", "--short", "HEAD"], {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    commit = "";
  }
}
if (!commit) commit = "dev";

// 静态资源内容指纹：只改 CSS/JS 也一定会换 ?v=，不再靠手写 ASSET_VERSION 记得 bump。
// 固定文件顺序，避免文件系统返回顺序不同导致同一份内容算出两个版本。
const assetFiles = ["app.js", "favicon.svg", "site.css", "theme.js"];
const assetHash = crypto.createHash("sha256");
for (const name of assetFiles) {
  assetHash.update(name);
  assetHash.update("\0");
  assetHash.update(fs.readFileSync(path.join(root, "public", name)));
  assetHash.update("\0");
}
const assetVersion = `${version}-${assetHash.digest("hex").slice(0, 10)}`;

const target = path.join(root, "src", "version.js");
const body = `// 站点版本号：由 scripts/gen-version.mjs 在部署前写入，不要手改。
// 版本规则见仓库根 CONTRIBUTING.md「提交约定」。
export const SITE_VERSION = "${version}";
export const SITE_COMMIT = "${commit}";
export const ASSET_VERSION = "${assetVersion}";
`;
fs.writeFileSync(target, body, "utf8");
console.log(`[version] src/version.js -> ${version} @ ${commit}`);

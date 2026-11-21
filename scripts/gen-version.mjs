// 从 package.json 生成 src/version.ts，保证 UI 版本号与发布版本一致
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf-8"));
writeFileSync(
  join(root, "src", "version.ts"),
  `// 自动生成，请勿手改\nexport const PLUGIN_VERSION = "${pkg.version}";\n`,
);
console.log(`version.ts -> ${pkg.version}`);

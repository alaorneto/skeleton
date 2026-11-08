// Build reprodutível dos assets estáticos da interface SSR.
//
//   npm run build   gera o CSS compilado e copia os assets vendorizados
//   npm run check   gera em diretório temporário e falha se o resultado
//                   versionado em skeleton/web/static estiver desatualizado
//
// Saídas (todas versionadas; o wheel não depende de Node em runtime):
//   skeleton/web/static/skeleton/web/site.css   (interface SSR)
//   skeleton/web/static/skeleton/web/admin.css  (tema do admin Django)
//   skeleton/web/static/skeleton/web/vendor/flowbite-<versão>/flowbite.min.js
//   skeleton/web/static/skeleton/web/vendor/inter-<versão>/*.woff2
//   skeleton/web/static/skeleton/web/vendor/VENDOR.json
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repository = resolve(frontend, "..");
const staticRoot = join(repository, "skeleton", "web", "static", "skeleton", "web");
const check = process.argv.includes("--check");

const packageVersion = (name) => JSON.parse(readFileSync(require.resolve(`${name}/package.json`), "utf8")).version;
const packageDir = (name) => dirname(require.resolve(`${name}/package.json`));
const sha256 = (buffer) => createHash("sha256").update(buffer).digest("hex");
const lf = (text) => text.replace(/\r\n?/g, "\n");

const flowbite = packageVersion("flowbite");
const inter = packageVersion("@fontsource-variable/inter");
const tailwind = packageVersion("tailwindcss");

// Os tokens referenciam o diretório da fonte com a versão; impede deriva silenciosa.
const tokens = readFileSync(join(frontend, "src", "tokens.css"), "utf8");
if (!tokens.includes(`vendor/inter-${inter}/`)) {
  console.error(`src/tokens.css precisa referenciar vendor/inter-${inter}/ (versão instalada).`);
  process.exit(1);
}
// Os templates referenciam os assets vendorizados pela versão instalada.
for (const template of ["skeleton/web/templates/skeleton/web/base.html", "skeleton/web/templates/skeleton/web/components/admin-theme.html"]) {
  const text = readFileSync(join(repository, ...template.split("/")), "utf8");
  for (const match of text.matchAll(/vendor\/(flowbite|inter)-([0-9][^/]*)\//g)) {
    const expected = match[1] === "flowbite" ? flowbite : inter;
    if (match[2] !== expected) {
      console.error(`${template} referencia vendor/${match[1]}-${match[2]}/; versão instalada: ${expected}.`);
      process.exit(1);
    }
  }
}
// Entradas Tailwind: interface SSR e tema do admin Django.
const entries = ["site.css", "admin.css"];

function buildInto(outRoot) {
  const files = new Map();
  const cli = join(packageDir("@tailwindcss/cli"), "dist", "index.mjs");
  mkdirSync(outRoot, { recursive: true });
  for (const entry of entries) {
    const cssOut = join(outRoot, entry);
    const result = spawnSync(process.execPath, [cli, "-i", join(frontend, "src", entry), "-o", cssOut, "--minify"], {
      cwd: frontend,
      stdio: ["ignore", "inherit", "inherit"],
    });
    if (result.status !== 0) process.exit(result.status ?? 1);
    files.set(entry, Buffer.from(lf(readFileSync(cssOut, "utf8")).trimEnd() + "\n"));
  }

  // Flowbite JS: o mapa de fonte não é distribuído, então a referência sai
  // (ManifestStaticFilesStorage falharia ao resolver um .map ausente).
  const flowbiteJs = lf(readFileSync(join(packageDir("flowbite"), "dist", "flowbite.min.js"), "utf8"))
    .replace(/\n?\/\/# sourceMappingURL=\S+\s*$/, "")
    .trimEnd();
  files.set(
    `vendor/flowbite-${flowbite}/flowbite.min.js`,
    Buffer.from(`/*! Flowbite ${flowbite} | MIT License | https://flowbite.com */\n${flowbiteJs}\n`),
  );
  files.set(`vendor/flowbite-${flowbite}/LICENSE.md`, Buffer.from(lf(readFileSync(join(packageDir("flowbite"), "LICENSE.md"), "utf8"))));

  const interDir = packageDir("@fontsource-variable/inter");
  for (const subset of ["latin", "latin-ext"]) {
    const name = `inter-${subset}-wght-normal.woff2`;
    files.set(`vendor/inter-${inter}/${name}`, readFileSync(join(interDir, "files", name)));
  }
  files.set(`vendor/inter-${inter}/LICENSE`, Buffer.from(lf(readFileSync(join(interDir, "LICENSE"), "utf8"))));

  const manifest = {
    generated_by: "frontend/scripts/build.mjs",
    tailwindcss: tailwind,
    packages: {
      flowbite: { version: flowbite, license: "MIT", source: "npm:flowbite" },
      "@fontsource-variable/inter": { version: inter, license: "OFL-1.1", source: "npm:@fontsource-variable/inter" },
    },
    files: Object.fromEntries(
      [...files.entries()].filter(([name]) => name.startsWith("vendor/")).map(([name, data]) => [name, sha256(data)]),
    ),
  };
  files.set("vendor/VENDOR.json", Buffer.from(JSON.stringify(manifest, null, 2) + "\n"));
  return files;
}

function listVendor(root) {
  const base = join(root, "vendor");
  const found = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else found.push(relative(root, path).split("\\").join("/"));
    }
  };
  try {
    walk(base);
  } catch {
    return [];
  }
  return found;
}

const scratch = mkdtempSync(join(tmpdir(), "skeleton-frontend-"));
try {
  const files = buildInto(scratch);
  const stale = [];
  for (const [name, data] of files) {
    const target = join(staticRoot, name);
    let current = null;
    try {
      current = readFileSync(target);
    } catch {
      current = null;
    }
    // Compara sem CR para tolerar core.autocrlf em checkouts Windows.
    const same = current !== null && (name.endsWith(".woff2") ? current.equals(data) : lf(current.toString("utf8")) === data.toString("utf8"));
    if (same) continue;
    stale.push(name);
    if (!check) {
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, data);
    }
  }
  const orphans = listVendor(staticRoot).filter((name) => !files.has(name));
  if (check) {
    if (stale.length || orphans.length) {
      for (const name of stale) console.error(`desatualizado: skeleton/web/static/skeleton/web/${name}`);
      for (const name of orphans) console.error(`órfão: skeleton/web/static/skeleton/web/${name}`);
      console.error("Execute `npm run build` em frontend/ e versione o resultado.");
      process.exit(1);
    }
    console.log(`Assets atualizados (Tailwind ${tailwind}, Flowbite ${flowbite}, Inter ${inter}).`);
  } else {
    for (const name of orphans) rmSync(join(staticRoot, name));
    console.log(`${stale.length} arquivo(s) gravado(s), ${orphans.length} órfão(s) removido(s).`);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}

// Build reprodutível dos assets estáticos da interface SSR.
//
//   npm run build   gera o CSS compilado e copia os assets vendorizados
//   npm run check   gera em diretório temporário e falha se o resultado
//                   versionado em skeleton/web/static estiver desatualizado
//
// Saídas (todas versionadas; o wheel não depende de Node em runtime):
//   skeleton/web/static/skeleton/web/site.css   (interface SSR)
//   skeleton/web/static/skeleton/web/vendor/flowbite-<versão>/flowbite.min.js
//   skeleton/web/static/skeleton/web/vendor/ibm-plex-sans-<versão>/*.woff2
//   skeleton/web/static/skeleton/web/markdown_editor.js        (editor de texto formatado, #825)
//   skeleton/web/static/skeleton/web/markdown_editor.LICENSES.txt
//   skeleton/web/static/skeleton/web/vendor/VENDOR.json
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

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
const plex = packageVersion("@fontsource-variable/ibm-plex-sans");
const tailwind = packageVersion("tailwindcss");

// Os tokens referenciam o diretório da fonte com a versão; impede deriva silenciosa.
const tokens = readFileSync(join(frontend, "src", "tokens.css"), "utf8");
if (!tokens.includes(`vendor/ibm-plex-sans-${plex}/`)) {
  console.error(`src/tokens.css precisa referenciar vendor/ibm-plex-sans-${plex}/ (versão instalada).`);
  process.exit(1);
}
// Os templates referenciam os assets vendorizados pela versão instalada.
for (const template of ["skeleton/web/templates/skeleton/web/base.html"]) {
  const text = readFileSync(join(repository, ...template.split("/")), "utf8");
  for (const match of text.matchAll(/vendor\/(flowbite|ibm-plex-sans)-([0-9][^/]*)\//g)) {
    const expected = match[1] === "flowbite" ? flowbite : plex;
    if (match[2] !== expected) {
      console.error(`${template} referencia vendor/${match[1]}-${match[2]}/; versão instalada: ${expected}.`);
      process.exit(1);
    }
  }
}
// O CodeMirror injeta o próprio CSS por style-mod, que no documento usa um <style> (só em shadow
// root usa folha adotada) e seria bloqueado pela CSP (style-src sem 'unsafe-inline'). Este ajuste
// faz o style-mod usar sempre a folha construída e adotada (CSSOM, que a CSP não bloqueia). O build
// falha se o trecho mudar numa atualização do style-mod, para o ajuste ser revisto.
const STYLE_MOD_TAG = "if (!root.head && root.adoptedStyleSheets && win.CSSStyleSheet) {";
const adoptedStyleSheets = {
  name: "style-mod-adopted-sheets",
  setup(context) {
    context.onLoad({ filter: /[\\/]style-mod[\\/]src[\\/]style-mod\.js$/ }, (args) => {
      const source = readFileSync(args.path, "utf8");
      if (!source.includes(STYLE_MOD_TAG)) {
        return { errors: [{ text: "style-mod mudou: revise o ajuste das folhas adotadas (frontend/scripts/build.mjs)." }] };
      }
      return { contents: source.replace(STYLE_MOD_TAG, "if (root.adoptedStyleSheets && win.CSSStyleSheet) {"), loader: "js" };
    });
  },
};

// Os pacotes de node_modules que entraram num bundle do esbuild, com versão e licença.
function bundledPackages(metafile) {
  const dirs = new Map();
  for (const input of Object.keys(metafile.inputs)) {
    const path = input.split("\\").join("/");
    const match = /^(.*node_modules\/((?:@[^/]+\/)?[^/]+))\//.exec(path);
    if (match) dirs.set(match[1], match[2]);
  }
  return [...dirs.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([relativeDir, name]) => {
      const dir = resolve(frontend, relativeDir);
      const meta = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
      const licenseFile = readdirSync(dir).find((entry) => /^licen[cs]e(-mit)?(\.(md|txt))?$/i.test(entry));
      if (!licenseFile) {
        console.error(`${name}: sem arquivo de licença para distribuir.`);
        process.exit(1);
      }
      return { name, version: meta.version, license: meta.license, text: lf(readFileSync(join(dir, licenseFile), "utf8")).trimEnd() };
    });
}

// Entrada Tailwind da interface SSR.
const entries = ["site.css"];

async function buildInto(outRoot) {
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
  // Favicon padrão do núcleo (#485): copiado de frontend/src, nunca editado em static/.
  files.set("favicon.svg", Buffer.from(lf(readFileSync(join(frontend, "src", "favicon.svg"), "utf8")).trimEnd() + "\n"));

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

  // Editor de texto formatado (#825): src/markdown-editor e as bibliotecas que ele usa, em módulos ES
  // minificados. O modo Markdown (CodeMirror) fica num pedaço à parte, carregado por import() só
  // quando alguém o abre; os pedaços têm o hash do conteúdo no nome. Sem mapa de fonte; as licenças
  // vão num arquivo ao lado.
  const editor = await build({
    plugins: [adoptedStyleSheets],
    entryPoints: { markdown_editor: join(frontend, "src", "markdown-editor", "index.js") },
    absWorkingDir: frontend,
    outdir: join(frontend, "dist"),
    bundle: true,
    splitting: true,
    format: "esm",
    target: ["es2022"],
    minify: true,
    legalComments: "none",
    charset: "utf8",
    chunkNames: "markdown_editor-[name]-[hash]",
    banner: { js: "/*! Editor de texto formatado do Skeleton | bibliotecas e licenças: markdown_editor.LICENSES.txt */" },
    metafile: true,
    write: false,
    logLevel: "warning",
  });
  for (const output of editor.outputFiles) {
    files.set(basename(output.path), Buffer.from(`${lf(output.text).trimEnd()}\n`));
  }
  const editorPackages = bundledPackages(editor.metafile);
  files.set(
    "markdown_editor.LICENSES.txt",
    Buffer.from(
      editorPackages
        .map((item) => `${item.name} ${item.version} (${item.license})\n\n${item.text}\n`)
        .join(`\n${"-".repeat(72)}\n\n`),
    ),
  );

  const plexDir = packageDir("@fontsource-variable/ibm-plex-sans");
  for (const style of ["normal", "italic"]) {
    for (const subset of ["latin", "latin-ext"]) {
      const name = `ibm-plex-sans-${subset}-wght-${style}.woff2`;
      files.set(`vendor/ibm-plex-sans-${plex}/${name}`, readFileSync(join(plexDir, "files", name)));
    }
  }
  files.set(`vendor/ibm-plex-sans-${plex}/LICENSE`, Buffer.from(lf(readFileSync(join(plexDir, "LICENSE"), "utf8"))));

  const manifest = {
    generated_by: "frontend/scripts/build.mjs",
    tailwindcss: tailwind,
    packages: {
      flowbite: { version: flowbite, license: "MIT", source: "npm:flowbite" },
      "@fontsource-variable/ibm-plex-sans": {
        version: plex,
        license: "OFL-1.1",
        source: "npm:@fontsource-variable/ibm-plex-sans",
      },
      ...Object.fromEntries(
        editorPackages.map((item) => [item.name, { version: item.version, license: item.license, source: `npm:${item.name}`, bundled_in: "markdown_editor.js" }]),
      ),
    },
    files: Object.fromEntries(
      [...files.entries()]
        .filter(([name]) => name.startsWith("vendor/") || name.startsWith("markdown_editor."))
        .map(([name, data]) => [name, sha256(data)]),
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
  const files = await buildInto(scratch);
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
  // Órfãos: arquivos de vendor/ e pedaços antigos do editor que o build não gera mais.
  const editorFiles = readdirSync(staticRoot).filter((name) => name.startsWith("markdown_editor"));
  const orphans = [...listVendor(staticRoot), ...editorFiles].filter((name) => !files.has(name));
  if (check) {
    if (stale.length || orphans.length) {
      for (const name of stale) console.error(`desatualizado: skeleton/web/static/skeleton/web/${name}`);
      for (const name of orphans) console.error(`órfão: skeleton/web/static/skeleton/web/${name}`);
      console.error("Execute `npm run build` em frontend/ e versione o resultado.");
      process.exit(1);
    }
    console.log(`Assets atualizados (Tailwind ${tailwind}, Flowbite ${flowbite}, IBM Plex Sans ${plex}).`);
  } else {
    for (const name of orphans) rmSync(join(staticRoot, name));
    console.log(`${stale.length} arquivo(s) gravado(s), ${orphans.length} órfão(s) removido(s).`);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}

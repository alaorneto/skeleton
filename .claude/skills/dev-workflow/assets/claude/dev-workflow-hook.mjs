#!/usr/bin/env node
// Hooks do Claude Code para o skill dev-workflow (fluxo issue-first e versão).
//   prompt:        UserPromptSubmit; no modo de planejamento, ou quando o pedido fala de issue,
//                  PR, commit, branch, versão ou release, manda carregar o skill.
//   plan-mode:     PostToolUse de EnterPlanMode; o agente entrou no modo de planejamento.
//   plan-accepted: PostToolUse de ExitPlanMode; só dispara quando o plano é aceito.
//   pre-shell:     PreToolUse de Bash/PowerShell; guarda git commit e gh pr create/new.
// Falhas internas nunca bloqueiam: o hook commit-msg e o CI são as barreiras finais.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

// Os mesmos caminhos que scripts/devflow/config.py aceita (CONFIG_CANDIDATES).
const CONFIG_PATHS = [".github/dev-workflow.toml", "dev-workflow.toml", ".dev-workflow.toml"];
const ISSUE_REF = /\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\b\s*:?\s+(?:[\w.-]+\/[\w.-]+)?#\d+/i;

function readInput() {
  try {
    return JSON.parse(readFileSync(0, "utf8") || "{}");
  } catch {
    return {};
  }
}

function emit(output) {
  process.stdout.write(JSON.stringify(output));
  process.exit(0);
}

function deny(reason) {
  emit({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: reason,
    },
  });
}

const PLAN_MODE =
  "Modo de planejamento: carregue o skill `dev-workflow` antes de escrever o plano. O plano termina " +
  "com a seção 'Itens do GitHub': para cada item, label de tipo, título, critérios de aceite, plano de " +
  "testes e documentação a atualizar. Quando o plano for aceito, as issues são criadas antes de " +
  "qualquer edição.";
const FLOW =
  "Este projeto segue o skill `dev-workflow` para issue, branch, commit, PR, versão, release e " +
  "fechamento de issue: se o pedido envolve um deles ou vai mudar arquivo versionado, carregue o " +
  "skill antes de agir. Perguntas e investigações sem mudança de arquivo não precisam dele.";
// Palavras que indicam uma etapa do fluxo, em português e em inglês; o skill decide o resto.
const FLOW_WORDS =
  /(?:^|[^\p{L}\d])(?:pull[\s-]?requests?|PRs?|issues?|epics?|commit\p{L}*|branch\p{L}*|merge\p{L}*|mescl\p{L}*|pr[eé]-?releases?|releases?|milestones?|vers(?:ão|ao|ões|oes|ion\p{L}*)|semver|changelog|tags?|bump|hotfix|plan(?:o|os|s|ned|ning)?|planej\p{L}*)(?![\p{L}\d])/iu;

function context(eventName, text) {
  emit({ hookSpecificOutput: { hookEventName: eventName, additionalContext: text } });
}

function promptSubmitted(input) {
  if (input?.permission_mode === "plan") context("UserPromptSubmit", PLAN_MODE);
  if (FLOW_WORDS.test(String(input?.prompt ?? ""))) context("UserPromptSubmit", FLOW);
  process.exit(0);
}

function planAccepted() {
  emit({
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext:
        "Plano aceito. Antes de qualquer edição de arquivo, invoque o skill `dev-workflow` " +
        "(fase 2): crie no GitHub as issues da seção 'Itens do GitHub' do plano, cada uma com " +
        "exatamente um label de tipo e todas as seções do modelo (critérios de aceite, plano de " +
        "testes, documentação a atualizar, dependências, definição de pronto), e um epic quando " +
        "houver 2 ou mais itens, com as filhas como sub-issues nativos e as dependências como " +
        "'blocked by' (scripts/github_issues.py sync-epic <E> do skill). Mostre ao usuário a tabela " +
        "com os links e só então implemente, começando pela primeira issue, em branch " +
        "<tipo>/<N>-<slug> (scripts/github_issues.py start <N>).",
    },
  });
}

// --- Separação do comando, como o shell faria -------------------------------------------------
// O comando vira segmentos (separados por `;`, `&&`, `||`, `|`, `&`, parênteses e quebra de linha
// fora de aspas) e cada segmento vira palavras: aspas e escapes respeitados, comentários
// ignorados, redirecionamentos de saída descartados, heredocs e `<` guardados como a entrada.
const HEREDOC = /^<<-?[ \t]*(?:'([^']*)'|"([^"]*)"|([^\s;&|<>()]+))/;

function readHeredoc(text, start, delimiter) {
  const lines = [];
  let pos = start;
  while (pos < text.length) {
    let end = text.indexOf("\n", pos);
    if (end < 0) end = text.length;
    const line = text.slice(pos, end);
    pos = end + 1;
    if (line.trim() === delimiter) return { body: lines.join("\n"), next: pos };
    lines.push(line);
  }
  return { body: lines.join("\n"), next: text.length };
}

// Índice do `)` que fecha a substituição `$(` cujo `(` está em `open` (aspas e heredocs dentro).
function substitutionEnd(text, open) {
  const heredocs = [];
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\\") i++;
    else if (ch === "'") i = text.indexOf("'", i + 1) < 0 ? text.length : text.indexOf("'", i + 1);
    else if (ch === '"') {
      for (i++; i < text.length && text[i] !== '"'; i++) {
        if (text[i] === "\\") i++;
        else if (text[i] === "$" && text[i + 1] === "(") i = substitutionEnd(text, i + 1);
      }
    } else if (ch === "<" && text[i + 1] === "<" && text[i + 2] !== "<") {
      const heredoc = HEREDOC.exec(text.slice(i));
      if (heredoc) {
        heredocs.push(heredoc[1] ?? heredoc[2] ?? heredoc[3]);
        i += heredoc[0].length - 1;
      }
    } else if (ch === "\n" && heredocs.length) {
      let next = i + 1;
      for (const delimiter of heredocs.splice(0)) next = readHeredoc(text, next, delimiter).next;
      i = next - 1;
    } else if (ch === "(") depth++;
    else if (ch === ")" && --depth === 0) return i;
  }
  return text.length;
}

function splitCommand(text, powershell) {
  const escape = powershell ? "`" : "\\";
  const segments = [];
  const pending = []; // heredocs à espera da próxima quebra de linha
  let segment;
  let word = null;
  let target = null; // para onde vai a próxima palavra: argumento, entrada ou saída
  const open = (start) => {
    segment = { start, words: [], stdin: [], input: null, op: "" };
    target = null;
  };
  const flush = () => {
    if (word === null) return;
    if (target === "stdin") segment.stdin.push(word);
    else if (target === "input") segment.input = word;
    else if (target !== "output") segment.words.push(word);
    word = null;
    target = null;
  };
  const close = (end, op, next = end + op.length) => {
    flush();
    segment.text = text.slice(segment.start, end);
    segment.op = op;
    segments.push(segment);
    open(next);
  };
  const substitution = (i) => {
    const end = substitutionEnd(text, i + 1);
    word = (word ?? "") + text.slice(i, end + 1);
    return end;
  };

  open(0);
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "'") {
      const end = text.indexOf("'", i + 1);
      const stop = end < 0 ? text.length : end;
      word = (word ?? "") + text.slice(i + 1, stop);
      i = stop;
    } else if (ch === '"') {
      word ??= "";
      for (i++; i < text.length && text[i] !== '"'; i++) {
        if (text[i] === "$" && text[i + 1] === "(") {
          i = substitution(i);
          continue;
        }
        if (text[i] === escape && (powershell || '$`"\\\n'.includes(text[i + 1] ?? "x"))) i++;
        word += text[i] ?? "";
      }
    } else if (ch === escape) {
      if (text[i + 1] !== "\n") word = (word ?? "") + (text[i + 1] ?? "");
      i++;
    } else if (ch === "$" && text[i + 1] === "(") {
      i = substitution(i);
    } else if (ch === "#" && word === null) {
      const end = text.indexOf("\n", i);
      i = (end < 0 ? text.length : end) - 1;
    } else if (ch === " " || ch === "\t" || ch === "\r") {
      flush();
    } else if (ch === "\n") {
      let next = i + 1;
      for (const { owner, delimiter } of pending.splice(0)) {
        const heredoc = readHeredoc(text, next, delimiter);
        owner.stdin.push(heredoc.body);
        next = heredoc.next;
      }
      close(i, "\n", next);
      i = next - 1;
    } else if (ch === "<") {
      flush();
      const heredoc = text[i + 1] === "<" && text[i + 2] !== "<" ? HEREDOC.exec(text.slice(i)) : null;
      if (heredoc) {
        pending.push({ owner: segment, delimiter: heredoc[1] ?? heredoc[2] ?? heredoc[3] });
        i += heredoc[0].length - 1;
      } else if (text[i + 1] === "<") {
        i += 2; // here-string `<<<`
        target = "stdin";
      } else {
        target = "input";
      }
    } else if (ch === ">") {
      if (word !== null && /^\d+$/.test(word)) word = null; // `2>`: o número é do redirecionamento
      flush();
      while (text[i + 1] === ">" || text[i + 1] === "|") i++;
      if (text[i + 1] === "&") {
        i++; // `2>&1`
        while (/[\d-]/.test(text[i + 1] ?? "")) i++;
      } else {
        target = "output";
      }
    } else if (ch === "&") {
      if (text[i + 1] === "&") close(i++, "&&");
      else if (text[i + 1] === ">") flush(); // `&>`: o `>` trata o resto
      else close(i, "&");
    } else if (ch === "|") {
      if (text[i + 1] === "|") close(i++, "||");
      else {
        close(i, "|");
        if (text[i + 1] === "&") i++;
      }
    } else if (ch === ";" || ch === "(" || ch === ")") {
      close(i, ch);
    } else {
      word = (word ?? "") + ch;
    }
  }
  close(text.length, "");
  return segments;
}

// Programa do segmento e seus argumentos, sem atribuições `VAR=x` e prefixos como `env`.
const PREFIXES = new Set(["command", "builtin", "exec", "env", "nohup", "sudo", "time", "then", "do", "else", "if", "while", "until", "{", "!"]);

function programOf(words) {
  let k = 0;
  while (k < words.length && (PREFIXES.has(words[k]) || /^[A-Za-z_]\w*=/.test(words[k]))) k++;
  const name = (words[k] ?? "").split(/[\\/]/).pop().toLowerCase().replace(/\.exe$/, "");
  return { name, args: words.slice(k + 1) };
}

function splitLong(arg) {
  const eq = arg.indexOf("=");
  return eq < 0 ? [arg, undefined] : [arg.slice(0, eq), arg.slice(eq + 1)];
}

// O git aceita prefixo único de opção longa (`--no-verif`); o gh não.
function resolveLong(name, known) {
  if (known.includes(name)) return name;
  const matches = known.filter((option) => option.startsWith(name));
  return matches.length === 1 ? matches[0] : name;
}

// --- git commit ---------------------------------------------------------------------------------
const GIT_GLOBAL_VALUE = ["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix", "--attr-source"];
const COMMIT_VALUE = [
  "--message", "--file", "--reuse-message", "--reedit-message", "--fixup", "--squash", "--author",
  "--date", "--cleanup", "--template", "--trailer", "--pathspec-from-file",
];
const COMMIT_LONG = [
  ...COMMIT_VALUE, "--no-verify", "--verify", "--verbose", "--no-verbose", "--all", "--amend",
  "--allow-empty", "--allow-empty-message", "--dry-run", "--edit", "--no-edit", "--include", "--only",
  "--patch", "--quiet", "--signoff", "--no-signoff", "--status", "--no-status", "--untracked-files",
  "--gpg-sign", "--no-gpg-sign", "--reset-author", "--interactive", "--short", "--branch",
  "--porcelain", "--long", "--null", "--no-post-rewrite", "--pathspec-file-nul",
];

// Opções globais do git: o subcomando, o diretório de `-C` e o desvio de `core.hooksPath`.
function gitInvocation(args, cwd) {
  let i = 0;
  let hooksPath = false;
  for (; i < args.length && args[i].startsWith("-"); i++) {
    let [name, value] = args[i].startsWith("--") ? splitLong(args[i]) : [args[i], undefined];
    if (GIT_GLOBAL_VALUE.includes(name) && value === undefined) value = args[++i] ?? "";
    if (name === "-C") cwd = path.resolve(cwd, value);
    if ((name === "-c" || name === "--config-env") && /^core\.hookspath(?:=|$)/i.test(value)) hooksPath = true;
  }
  return { subcommand: args[i], args: args.slice(i + 1), cwd, hooksPath };
}

// `--no-verify`/`-n` nas opções do commit; valores de `-m`, `-F` etc. não são lidos como opção.
function skipsHooks(args) {
  let skip = false;
  for (let i = 0; i < args.length && args[i] !== "--"; i++) {
    const arg = args[i];
    if (arg.startsWith("--")) {
      const [name, value] = splitLong(arg);
      const option = resolveLong(name, COMMIT_LONG);
      if (option === "--no-verify" || option === "--verify") skip = option === "--no-verify";
      else if (COMMIT_VALUE.includes(option) && value === undefined) i++;
    } else if (arg.length > 1 && arg.startsWith("-")) {
      for (let k = 1; k < arg.length; k++) {
        if (arg[k] === "n") skip = true;
        else if ("mFCct".includes(arg[k])) {
          if (k === arg.length - 1) i++; // o valor é a próxima palavra
          break;
        } else if ("Su".includes(arg[k])) break; // valor opcional colado
      }
    }
  }
  return skip;
}

function checkCommit(invocation) {
  if (invocation.subcommand !== "commit") return;
  if (invocation.hooksPath) {
    deny(
      "Este projeto não aceita desviar os hooks do commit (`-c core.hooksPath=`): o hook commit-msg " +
        "garante o vínculo com a issue (skill dev-workflow).",
    );
  }
  if (skipsHooks(invocation.args)) {
    deny(
      "Este projeto não aceita `git commit --no-verify`/`-n`: o hook commit-msg garante o vínculo " +
        "com a issue (skill dev-workflow).",
    );
  }
  if (workflowAdopted(invocation.cwd) && !commitMsgHookInstalled(invocation.cwd)) {
    deny(
      "O hook commit-msg não está instalado neste checkout. Rode `pre-commit install` (se o projeto usa " +
        "pre-commit) ou `scripts/bootstrap.py install-hook` do skill dev-workflow e repita o commit. " +
        "Todo commit precisa de `Refs #N`.",
    );
  }
}

// --- gh pr create / gh pr new ---------------------------------------------------------------------
const PR_VALUE = [
  "--title", "--body", "--body-file", "--base", "--head", "--assignee", "--reviewer", "--label",
  "--milestone", "--project", "--repo", "--template", "--recover",
];
const PR_SHORT_VALUE = "tbFBHarlmpRT";

// `--fill` (e variantes) e a última origem do corpo: `{ text }` ou `{ file }`.
function prOptions(args) {
  const found = { fill: false, body: null };
  const take = (name, value) => {
    if (name === "--body" || name === "-b") found.body = { text: value };
    if (name === "--body-file" || name === "-F") found.body = { file: value };
  };
  for (let i = 0; i < args.length && args[i] !== "--"; i++) {
    const arg = args[i];
    if (arg.startsWith("--")) {
      const [name, value] = splitLong(arg);
      if (/^--fill(?:-first|-verbose)?$/.test(name)) found.fill = value === undefined || value === "true";
      else if (PR_VALUE.includes(name)) take(name, value ?? args[++i] ?? "");
    } else if (arg.length > 1 && arg.startsWith("-")) {
      for (let k = 1; k < arg.length; k++) {
        if (arg[k] === "f") found.fill = true;
        else if (PR_SHORT_VALUE.includes(arg[k])) {
          const rest = arg.slice(k + 1);
          take(`-${arg[k]}`, rest ? rest.replace(/^=/, "") : args[++i] ?? "");
          break;
        }
      }
    }
  }
  return found;
}

// Entrada do segmento para `--body-file -`: heredoc, `< arquivo` ou o que vem pelo `|`.
function stdinOf(segments, index, cwd) {
  const segment = segments[index];
  if (segment.stdin.length) return segment.stdin.join("\n");
  if (segment.input !== null) return readFileSync(path.resolve(cwd, segment.input), "utf8");
  const piped = [];
  for (let j = index - 1; j >= 0 && segments[j].op === "|"; j--) piped.unshift(segments[j].text, ...segments[j].stdin);
  return piped.join("\n");
}

function checkPullRequest(options, segments, index, cwd) {
  if (options.fill && !options.body) {
    deny(
      "Não use `gh pr create --fill`: o corpo segue .github/pull_request_template.md e cita `Closes #N` " +
        "(skill dev-workflow, fase 5).",
    );
  }
  let body = "";
  try {
    if (options.body?.file === "-") body = stdinOf(segments, index, cwd);
    else if (options.body && "file" in options.body) body = readFileSync(path.resolve(cwd, options.body.file), "utf8");
    else body = options.body?.text ?? "";
  } catch {
    deny("Não consegui ler o arquivo de --body-file do `gh pr create`. Confira o caminho.");
  }
  if (!ISSUE_REF.test(body)) {
    deny(
      "O corpo do PR precisa referenciar a issue: `Closes #N` (entrega completa) ou `Refs #N` " +
        "(parcial). Carregue o skill dev-workflow (fase 5) e use o modelo de PR dele.",
    );
  }
}

function git(cwd, ...args) {
  return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
}

// O fluxo vale na branch cujo HEAD já versiona a configuração do skill; branches
// anteriores à adoção não são bloqueadas.
function workflowAdopted(cwd) {
  try {
    return git(cwd, "ls-tree", "--full-tree", "--name-only", "HEAD", "--", ...CONFIG_PATHS) !== "";
  } catch {
    return false;
  }
}

function commitMsgHookInstalled(cwd) {
  try {
    return existsSync(path.resolve(cwd, git(cwd, "rev-parse", "--git-path", "hooks/commit-msg")));
  } catch {
    return true; // fora de um repositório git: nada a guardar aqui
  }
}

// Cada segmento é conferido sozinho; `cd` muda o diretório dos segmentos seguintes.
function preShell(input) {
  const command = input?.tool_input?.command;
  if (typeof command !== "string") process.exit(0);
  let cwd = input?.cwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();
  const segments = splitCommand(command, input?.tool_name === "PowerShell");
  segments.forEach((segment, index) => {
    const { name, args } = programOf(segment.words);
    if (["cd", "pushd", "set-location", "sl"].includes(name) && args[0] && !args[0].startsWith("-")) {
      cwd = path.resolve(cwd, args[0]);
    }
    if (name === "git") checkCommit(gitInvocation(args, cwd));
    if (name === "gh" && args[0] === "pr" && (args[1] === "create" || args[1] === "new")) {
      checkPullRequest(prOptions(args.slice(2)), segments, index, cwd);
    }
  });
  process.exit(0);
}

const mode = process.argv[2];
const input = readInput();
try {
  if (mode === "prompt") promptSubmitted(input);
  if (mode === "plan-mode") context("PostToolUse", PLAN_MODE);
  if (mode === "plan-accepted") planAccepted();
  if (mode === "pre-shell") preShell(input);
} catch {
  // Erro interno não bloqueia o trabalho; as barreiras finais são commit-msg e CI.
}
process.exit(0);

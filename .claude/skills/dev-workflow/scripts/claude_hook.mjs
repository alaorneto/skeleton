#!/usr/bin/env node
// Hooks do Claude Code do skill dev-workflow (fluxo issue-first; references/mechanisms.md).
//   plan-accepted: PostToolUse de ExitPlanMode; só dispara quando o plano é aceito.
//   pre-shell:     PreToolUse de Bash/PowerShell; guarda git commit e gh pr create.
// Falhas internas nunca bloqueiam: o hook commit-msg e o CI são as barreiras finais.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

// `git [opções globais] commit`, sem confundir com `git log --grep commit`.
const GIT_COMMIT = /\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+commit\b/;
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

function planAccepted() {
  emit({
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext:
        "Plano aceito. Antes de qualquer edição de arquivo, invoque o skill `dev-workflow` " +
        "(fase 2): crie no GitHub as issues da seção 'Itens do GitHub' do plano, cada uma com " +
        "exatamente um label de tipo, todas as seções do modelo (critérios de aceite, plano de " +
        "testes, documentação a atualizar, segurança, dependências, definição de pronto) e um " +
        "epic quando houver 2 ou mais itens, com as filhas como sub-issues nativos e as dependências " +
        "como 'blocked by' (github_issues.py sync-epic <E>, no diretório do skill). Mostre ao usuário a tabela " +
        "com os links e só então " +
        "implemente, começando pela primeira issue, em branch <tipo>/<N>-<slug>.",
    },
  });
}

function bodyOf(command, cwd) {
  const file = command.match(/(?:--body-file|-F)\s+(?:"([^"]+)"|'([^']+)'|(\S+))/);
  if (!file) return command;
  try {
    return readFileSync(path.resolve(cwd, file[1] ?? file[2] ?? file[3]), "utf8");
  } catch {
    return null;
  }
}

function git(cwd, ...args) {
  return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
}

// O fluxo vale na branch cujo HEAD já versiona a configuração do skill (ou o hook issue-reference
// do pre-commit); branches anteriores à adoção não são bloqueadas.
function workflowAdopted(cwd) {
  for (const [file, marker] of [
    [".github/dev-workflow.toml", ""],
    [".pre-commit-config.yaml", "id: issue-reference"],
  ]) {
    try {
      if (git(cwd, "show", `HEAD:${file}`).includes(marker)) return true;
    } catch {
      // arquivo ausente nesta branch
    }
  }
  return false;
}

function commitMsgHookInstalled(cwd) {
  try {
    return existsSync(path.resolve(cwd, git(cwd, "rev-parse", "--git-path", "hooks/commit-msg")));
  } catch {
    return true; // fora de um repositório git: nada a guardar aqui
  }
}

function preShell(input) {
  const command = String(input?.tool_input?.command ?? "");
  const cwd = input?.cwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();

  const commit = command.match(GIT_COMMIT);
  if (commit) {
    // Só as opções do próprio commit, até o primeiro separador de comando.
    const options = command.slice(commit.index + commit[0].length).split(/[\n|;&]/)[0];
    if (/(?:^|\s)--no-verify\b/.test(options) || /(?:^|\s)-[a-zA-Z]*n[a-zA-Z]*(?=\s|$)/.test(options)) {
      deny(
        "Este projeto não aceita `git commit --no-verify`/`-n`: o hook commit-msg garante o vínculo " +
          "com a issue (skill dev-workflow).",
      );
    }
    if (workflowAdopted(cwd) && !commitMsgHookInstalled(cwd)) {
      deny(
        "O hook commit-msg não está instalado neste checkout. Rode `bootstrap.py init` do skill dev-workflow " +
          "(ou `pre-commit install`, se o projeto usa pre-commit) e repita o commit. Todo commit precisa de `Refs #N`.",
      );
    }
  }

  if (/\bgh\s+pr\s+create\b/.test(command)) {
    if (/\s--fill\b/.test(command) && !/--body/.test(command)) {
      deny("Não use `gh pr create --fill`: o corpo segue .github/pull_request_template.md e cita `Closes #N`.");
    }
    const body = bodyOf(command, cwd);
    if (body === null) {
      deny("Não consegui ler o arquivo de --body-file do `gh pr create`. Confira o caminho.");
    }
    if (!ISSUE_REF.test(body)) {
      deny(
        "O corpo do PR precisa referenciar a issue: `Closes #N` (entrega completa) ou `Refs #N` " +
          "(parcial). Use o modelo de PR do skill dev-workflow.",
      );
    }
  }
  process.exit(0);
}

const mode = process.argv[2];
const input = readInput();
try {
  if (mode === "plan-accepted") planAccepted();
  if (mode === "pre-shell") preShell(input);
} catch {
  // Erro interno não bloqueia o trabalho; as barreiras finais são commit-msg e CI.
}
process.exit(0);

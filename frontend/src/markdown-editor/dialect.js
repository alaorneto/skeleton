// O dialeto fechado do texto formatado (#824), igual a skeleton/common/markdown.py.
//
// O servidor renderiza com markdown-it-py; o editor lê e escreve com markdown-it, o mesmo
// analisador. As duas configurações precisam ficar iguais: tests/fixtures/markdown/corpus.json
// é conferido pelos dois lados (tests/unit/test_markdown.py e frontend/tests/dialect.test.mjs).
import MarkdownIt from "markdown-it";

// A ordem é a da faixa de opções.
export const FEATURES = Object.freeze(["emphasis", "underline", "strike", "code", "heading", "lists", "quote", "link"]);
export const MAX_HEADING_LEVEL = 3;
export const LINK_REL = "nofollow noopener noreferrer";

const MAX_NESTING = 20;
const FEATURE_RULES = {
  emphasis: ["emphasis"],
  underline: ["underline"],
  strike: ["strikethrough"],
  code: ["backticks"],
  heading: ["heading"],
  lists: ["list"],
  quote: ["blockquote"],
  link: ["link"],
};
// Sempre ligadas: parágrafos, texto, quebras de linha, escapes com \ e entidades.
const BASE_RULES = ["paragraph", "text", "newline", "escape", "entity"];
const PLUS = 0x2b;

/** A lista canônica (na ordem de FEATURES); `null`/`undefined` é tudo. */
export function validateFeatures(features) {
  if (features == null) return [...FEATURES];
  if (typeof features === "string") throw new Error("Markdown features must be a sequence of names, not a string.");
  const names = [...features];
  const unknown = names.filter(name => !FEATURES.includes(name));
  if (unknown.length) throw new Error(`Unknown Markdown feature: ${[...new Set(unknown)].sort().join(", ")}.`);
  if (new Set(names).size !== names.length) throw new Error("Repeated Markdown feature.");
  return FEATURES.filter(name => names.includes(name));
}

/** `https://` com host, ou caminho local (`/…`, nunca `//` nem barra invertida). */
export function isAllowedLink(url) {
  if (typeof url !== "string" || /[\u0000-\u001f\\]/.test(url)) return false;
  if (url.startsWith("/")) return !url.startsWith("//");
  if (url.slice(0, 8).toLowerCase() !== "https://") return false;
  try {
    return Boolean(new URL(url).hostname);
  } catch {
    return false;
  }
}

const parsers = new Map();

/** O markdown-it do dialeto com só os recursos pedidos (instâncias reaproveitadas). */
export function createMarkdownIt(features) {
  const canonical = validateFeatures(features);
  const key = canonical.join(",");
  if (parsers.has(key)) return parsers.get(key);
  const md = new MarkdownIt("zero", { html: false, xhtmlOut: false, breaks: true, linkify: false });
  md.options.maxNesting = MAX_NESTING;
  md.inline.ruler.before("emphasis", "underline", underlineTokenize);
  md.inline.ruler2.before("emphasis", "underline", underlinePostProcess);
  md.core.ruler.push("skeleton_dialect", dialectCoreRule);
  md.validateLink = isAllowedLink;
  md.disable("underline"); // Uma regra nova começa ligada; só o recurso a liga.
  md.enable([...BASE_RULES, ...canonical.flatMap(name => FEATURE_RULES[name])]);
  parsers.set(key, md);
  return md;
}

/** O HTML do servidor, para conferir o corpus (o editor não usa o HTML). */
export function renderHtml(text, features) {
  return createMarkdownIt(features).render(text);
}

function dialectCoreRule(state) {
  for (const token of state.tokens) {
    if ((token.type === "heading_open" || token.type === "heading_close") && Number(token.tag.slice(1)) > MAX_HEADING_LEVEL) {
      token.tag = `h${MAX_HEADING_LEVEL}`;
    }
    if (token.type === "inline" && token.children) token.children = filterLinks(token.children);
  }
}

function filterLinks(children) {
  const kept = [];
  const refused = [];
  for (const child of children) {
    if (child.type === "link_open") {
      const allowed = isAllowedLink(child.attrGet("href") || "");
      refused.push(!allowed);
      if (!allowed) continue;
      child.attrSet("rel", LINK_REL);
    } else if (child.type === "link_close") {
      if (refused.pop()) continue;
    }
    kept.push(child);
  }
  return kept;
}

// ++sublinhado++: as regras de delimitador da ênfase do CommonMark, como no markdown-it-ins,
// mas renderizado como <u> (a mesma regra está em skeleton/common/markdown.py).
function underlineTokenize(state, silent) {
  const start = state.pos;
  if (silent || state.src.charCodeAt(start) !== PLUS) return false;
  const scanned = state.scanDelims(start, true);
  let length = scanned.length;
  if (length < 2) return false;
  if (length % 2) {
    const token = state.push("text", "", 0);
    token.content = "+";
    length -= 1;
  }
  for (let i = 0; i < length; i += 2) {
    const token = state.push("text", "", 0);
    token.content = "++";
    state.delimiters.push({
      marker: PLUS,
      length: 0,
      token: state.tokens.length - 1,
      end: -1,
      open: scanned.can_open,
      close: scanned.can_close,
    });
  }
  state.pos += scanned.length;
  return true;
}

function underlinePairs(state, delimiters) {
  const loneMarkers = [];
  for (const start of delimiters) {
    if (start.marker !== PLUS || start.end === -1) continue;
    const end = delimiters[start.end];
    for (const [index, type, nesting] of [[start.token, "u_open", 1], [end.token, "u_close", -1]]) {
      const token = state.tokens[index];
      token.type = type;
      token.tag = "u";
      token.nesting = nesting;
      token.markup = "++";
      token.content = "";
    }
    const previous = state.tokens[end.token - 1];
    if (previous.type === "text" && previous.content === "+") loneMarkers.push(end.token - 1);
  }
  // Uma sequência ímpar vira "+" + "++" + "++": o marcador solto vai para depois dos fechamentos.
  while (loneMarkers.length) {
    const index = loneMarkers.pop();
    let target = index + 1;
    while (target < state.tokens.length && state.tokens[target].type === "u_close") target += 1;
    target -= 1;
    if (index !== target) [state.tokens[index], state.tokens[target]] = [state.tokens[target], state.tokens[index]];
  }
}

function underlinePostProcess(state) {
  underlinePairs(state, state.delimiters);
  for (const meta of state.tokens_meta) {
    if (meta && meta.delimiters) underlinePairs(state, meta.delimiters);
  }
}

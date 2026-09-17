const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
export const mentionPattern = token => new RegExp(`(?<![\\p{L}\\p{M}\\p{N}_@])${escape(token)}(?![\\p{L}\\p{M}\\p{N}_-])`, "gu");
export function activeMentions(brief, selections) {
  return Object.fromEntries(Object.entries(selections).filter(([token]) => mentionPattern(token).test(brief))
    .map(([token, character]) => [token, character.id]));
}
export function typedMentions(text) {
  return [...text.matchAll(/(?<![\p{L}\p{M}\p{N}_@])@[\p{L}\p{M}\p{N}_-]+/gu)].map(match => match[0]);
}

export function resolveTypedMentions(text, selections, characters) {
  const next = { ...selections };
  const eligible = characters.filter(c => c.status === "approved" && c.catalog_status === "customer" && c.display_name?.trim());
  for (const token of typedMentions(text)) {
    // Explicit picker selections win, including names with duplicate records.
    if (next[token]) continue;
    // Backend mention keys use Python's \w; combining marks need a picker token.
    if (/\p{M}/u.test(token)) continue;
    const name = token.slice(1).toLocaleLowerCase();
    const matches = eligible.filter(c => [mentionToken(c, {}).slice(1), c.display_name.trim().replace(/\s+/g, "_")]
      .some(alias => alias.toLocaleLowerCase() === name));
    if (matches.length === 1) next[token] = matches[0];
  }
  return next;
}

export function mentionSegments(text, selections) {
  const ranges = Object.keys(activeMentions(text, selections)).flatMap(token =>
    [...text.matchAll(mentionPattern(token))].map(match => ({ start: match.index, end: match.index + token.length, token }))
  ).sort((a, b) => a.start - b.start);
  let position = 0;
  const segments = [];
  for (const range of ranges) {
    if (range.start < position) continue;
    if (range.start > position) segments.push({ text: text.slice(position, range.start) });
    segments.push({ text: text.slice(range.start, range.end), token: range.token });
    position = range.end;
  }
  if (position < text.length) segments.push({ text: text.slice(position) });
  return segments;
}
export function mentionToken(character, selections) {
  const previous = Object.entries(selections).find(([, item]) => item.id === character.id);
  if (previous) return previous[0];
  const slug = character.display_name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-zA-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 90) || "Character";
  let token = `@${slug}`, count = 2;
  while (selections[token]) token = `@${slug}-${count++}`;
  return token;
}
const TIP_KEY = "merakify.character-mentions.discovered";
const JOBS_KEY = "merakify.character-mentions.jobs";
export function mentionsDiscovered() {
  try { return localStorage.getItem(TIP_KEY) === "yes" || Number(localStorage.getItem(JOBS_KEY) || 0) >= 3; } catch { return false; }
}
export function recordMentionJob() {
  try { localStorage.setItem(JOBS_KEY, String(Math.min(3, Number(localStorage.getItem(JOBS_KEY) || 0) + 1))); } catch { /* Optional discovery state. */ }
}
export function rememberMentions() {
  try { localStorage.setItem(TIP_KEY, "yes"); } catch { /* Storage may be disabled. */ }
}

const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
export const mentionPattern = token => new RegExp(`(?<![\\p{L}\\p{N}_@])${escape(token)}(?![\\p{L}\\p{N}_-])`, "gu");
export function activeMentions(brief, selections) {
  return Object.fromEntries(Object.entries(selections).filter(([token]) => mentionPattern(token).test(brief))
    .map(([token, character]) => [token, character.id]));
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

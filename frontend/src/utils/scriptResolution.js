import { caseFold } from "unicode-case-folding";

export function normalizeResolutionKey(value) {
  return caseFold(value.normalize("NFC")).normalize("NFC");
}

function foldedSourceWithOffsets(source) {
  const normalizedSource = source.normalize("NFC");
  let folded = "";
  const starts = [];
  const ends = [];

  for (let offset = 0; offset < normalizedSource.length;) {
    const codePoint = normalizedSource.codePointAt(offset);
    const character = String.fromCodePoint(codePoint);
    const nextOffset = offset + character.length;
    const foldedCharacter = caseFold(character);
    folded += foldedCharacter;
    for (let index = 0; index < foldedCharacter.length; index += 1) {
      starts.push(offset);
      ends.push(nextOffset);
    }
    offset = nextOffset;
  }

  return { normalizedSource, folded, starts, ends };
}

function casefoldOccurrences(source, name) {
  const { normalizedSource, folded, starts, ends } = foldedSourceWithOffsets(source);
  const needle = normalizeResolutionKey(name);
  if (!needle) return [];

  const occurrences = [];
  let searchFrom = 0;
  while (searchFrom <= folded.length - needle.length) {
    const matchIndex = folded.indexOf(needle, searchFrom);
    if (matchIndex === -1) break;
    const start = starts[matchIndex];
    const end = ends[matchIndex + needle.length - 1];
    occurrences.push({ start, value: normalizedSource.slice(start, end) });
    searchFrom = matchIndex + needle.length;
  }
  return occurrences;
}

function isAllCaps(value) {
  const upper = value.toLocaleUpperCase("und");
  const lower = value.toLocaleLowerCase("und");
  return upper !== lower && value === upper;
}

export function preferredDisplayName(source, extractedName) {
  const occurrences = casefoldOccurrences(source, extractedName);
  if (!occurrences.length) return extractedName.normalize("NFC");
  return (occurrences.find(({ value }) => !isAllCaps(value)) || occurrences[0]).value;
}

export function buildResolutionItems(source, names, type) {
  const grouped = new Map();
  for (const name of names) {
    const key = normalizeResolutionKey(name);
    if (!grouped.has(key)) grouped.set(key, name);
  }
  return Array.from(grouped, ([key, name]) => ({
    id: `${type}:${key}`,
    key,
    type,
    extractedName: name,
    displayName: preferredDisplayName(source, name),
  }));
}

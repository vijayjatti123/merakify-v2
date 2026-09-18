import test from 'node:test';
import assert from 'node:assert/strict';
import { preserveRefinedMentions, activeMentions } from '../src/utils/characterMentions.js';
const selections = {'@carpenter': {id:'vault-c', display_name:'Carpenter'}, '@yamaraj': {id:'vault-y', display_name:'Yamaraj'}};
const original = '@carpenter meets @yamaraj in the workshop.';
test('plain refined names restore original tags and exact submitted IDs', () => {
  const result = preserveRefinedMentions("Yamaraj finds Carpenter. Carpenter's chair holds.", original, selections);
  assert.equal(result, "@yamaraj finds @carpenter. @carpenter's chair holds.");
  assert.deepEqual(activeMentions(result, selections), {'@carpenter':'vault-c', '@yamaraj':'vault-y'});
});
test('existing tags are idempotent and casing does not change the selection', () => {
  assert.equal(preserveRefinedMentions(original, original, selections), original);
  assert.equal(preserveRefinedMentions('@Carpenter meets YAMARAJ.', original, selections), '@carpenter meets @yamaraj.');
});
test('omitted or translated names cannot silently remove a selected ID', () => {
  const text = preserveRefinedMentions('A man enters.', original, selections);
  assert.match(text, /Selected characters: @carpenter, @yamaraj/);
  assert.equal(Object.keys(activeMentions(text, selections)).length, 2);
});
test('stale selections removed before refinement are not restored', () => {
  assert.equal(preserveRefinedMentions('Carpenter rests.', 'Someone rests.', selections), 'Carpenter rests.');
});
test('explicit duplicate-name choice retains its ID; ambiguous bare names are blocked', () => {
  const selected = {'@Meera-2': {id:'second', display_name:'Meera'}};
  assert.deepEqual(activeMentions(preserveRefinedMentions('Meera smiles.', '@Meera-2 smiles.', selected), selected), {'@Meera-2':'second'});
  const both = {...selected, '@Meera': {id:'first', display_name:'Meera'}};
  assert.throws(() => preserveRefinedMentions('Meera smiles.', '@Meera meets @Meera-2', both), /original @tags/);
  assert.equal(preserveRefinedMentions('@Meera meets @Meera-2', '@Meera meets @Meera-2', both), '@Meera meets @Meera-2');
});
test('matches whole names, longer names first, without touching emails', () => {
  const names = {'@Ann':{id:'a',display_name:'Ann'}, '@Ann_Lee':{id:'b',display_name:'Ann Lee'}};
  assert.equal(preserveRefinedMentions('Ann Lee meets Ann, not Anna or x@Ann.', '@Ann meets @Ann_Lee', names), '@Ann_Lee meets @Ann, not Anna or x@Ann.');
});

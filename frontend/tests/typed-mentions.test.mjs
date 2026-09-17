import test from 'node:test';
import assert from 'node:assert/strict';
import { activeMentions, resolveTypedMentions, mentionSegments, mentionToken } from '../src/utils/characterMentions.js';
const meera = { id: 'meera-1', display_name: 'Meera', status: 'approved', catalog_status: 'customer' };

test('typed full names resolve to stable IDs without picker clicks, with punctuation and repeated uses', () => {
  const text = '@Meera walks in. @Meera: Hello!';
  const selected = resolveTypedMentions(text, {}, [meera]);
  assert.deepEqual(activeMentions(text, selected), { '@Meera': 'meera-1' });
  assert.equal(mentionSegments(text, selected).filter(p => p.token).length, 2);
  assert.equal(mentionSegments(text, selected).map(p => p.text).join(''), text);
});
test('partial names, emails and hidden/test characters never link', () => {
  assert.deepEqual(resolveTypedMentions('@Mee x@Meera @Test', {}, [meera, { ...meera, display_name:'Test', catalog_status:'test' }]), {});
});
test('duplicate names require selection, and explicit ID selections win', () => {
  const second = { ...meera, id:'meera-2' };
  assert.deepEqual(resolveTypedMentions('@Meera', {}, [meera, second]), {});
  assert.deepEqual(activeMentions('@Meera', resolveTypedMentions('@Meera', {'@Meera':second}, [meera, second])), {'@Meera':'meera-2'});
  assert.equal(mentionToken(second, {'@Meera':meera}), '@Meera-2');
});
test('case and underscore names work, longer names never match a prefix', () => {
  assert.deepEqual(activeMentions('@meera', resolveTypedMentions('@meera', {}, [meera])), {'@meera':'meera-1'});
  assert.deepEqual(resolveTypedMentions('@MeeraExtra', {}, [meera]), {});
  const full={...meera,display_name:'Meera Sharma'};
  assert.equal(resolveTypedMentions('@Meera_Sharma', {}, [full])['@Meera_Sharma'].id, meera.id);
});
test('editing away a mention removes it from payload and highlighting; text remains literal', () => {
  const text='<b>@MeeraExtra</b>\n@Meera';
  assert.deepEqual(activeMentions('@Other', {'@Meera':meera}), {});
  const parts=mentionSegments(text, {'@Meera':meera});
  assert.equal(parts.filter(p => p.token).length,1);
  assert.equal(parts.map(p => p.text).join(''),text);
});

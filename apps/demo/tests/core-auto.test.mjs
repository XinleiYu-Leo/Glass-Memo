import test from 'node:test';
import assert from 'node:assert/strict';
import { validateDataset, catalog, search, InputError } from '../core.mjs';

const fixture = () => ({ schema_version: 1, provenance: 'vision_pipeline',
  objects: [{id:'keys',label_zh:'钥匙',aliases:['钥匙']}],
  clips: [{id:'clip', label:'自动录像', started_at:'2026-10-23T07:34:21Z', duration_seconds:60,
    video_file:'data/auto/videos/run.mp4',poster_file:'data/auto/evidence/f1.jpg',run_key:'run-1',
    model:'test-model',prompt_version:'test-prompt',sampling_version:'test-sampling'}],
  observations: [1,2].map(n => ({id:`obs-${n}`, object_id:'keys',clip_id:'clip',frame_id:`f${n}`,run_key:'run-1',
    output_pts_seconds:n,state:'held',observation_zh:'手持钥匙',location_zh:'手中',evidence_file:`data/auto/evidence/f${n}.jpg`,
    provenance:'vision_pipeline',review_status:'auto_checked',human_ground_truth:null,
    model:'test-model',prompt_version:'test-prompt',sampling_version:'test-sampling',
    support_frame_ids:['f1','f2'],is_object_visible:true,use_for_last_seen:true,usable_as_clear_evidence:true,
    needs_review:false,identity:'match',hand_relation:'held',limitations:['尚未经人工确认。']})) });

test('automatic evidence has independent routes and never claims human review', () => {
  const data = validateDataset(fixture());
  const list = catalog(data, {source:'auto'});
  assert.equal(list.clips[0].video_url, '/auto/media/clip');
  assert.equal(list.observations[0].evidence_url, '/auto/evidence/obs-1');
  assert.match(list.provenance_label,/自动识别.*待本人确认/);
  const result = search(data,{query:'钥匙',source:'auto'});
  assert.equal(result.answers[0].observation.id,'obs-2');
  assert.match(result.answers[0].caveat,/自动识别.*尚未经人工确认/);
  assert.equal(result.answers[0].observation.video_url,'/auto/media/clip');
});

test('automatic clear evidence rejects unsupported claims and mixed run metadata', () => {
  for (const mutate of [
    o => {o.support_frame_ids=['f1','f1'];}, o=>{o.support_frame_ids=['other','another'];},
    o => {o.run_key='wrong-run';}, o=>{o.needs_review=true;}, o=>{o.state='placed';},
    o => {o.hand_relation='free';}, o=>{o.identity='uncertain';}, o=>{o.human_ground_truth=true;},
    o => {o.review_status='ai_reviewed';}, o=>{delete o.model;},
    o => {o.model='different';}, o=>{o.prompt_version='different';}, o=>{o.sampling_version='different';},
  ]) { const data=fixture(); mutate(data.observations[0]); assert.throws(()=>validateDataset(data),InputError); }
});

test('uncertain automatic records remain auxiliary and cannot answer last position', () => {
  const data=fixture();
  data.observations=data.observations.map(o=>({...o,state:'uncertain',needs_review:true,
    use_for_last_seen:false,usable_as_clear_evidence:false,support_frame_ids:[]}));
  validateDataset(data);
  assert.equal(search(data,{query:'钥匙',source:'auto'}).status,'no_evidence');
});

test('a last automatic sighting never implies the object stayed after observations stop', () => {
  const data=fixture();
  data.observations=data.observations.map(o=>({...o,state:'visible',hand_relation:'free',location_zh:'测试台 B'}));
  const answer=search(validateDataset(data),{query:'钥匙',source:'auto'}).answers[0];
  assert.match(answer.headline,/自动线索.*测试台 B.*后续去向未确认/);
  assert.equal(answer.history.length,2);
});

test('unrefined intervals cannot supply primary automatic evidence', () => {
  const data=fixture(); data.clips[0].unrefined_ranges=[{start:0,end:1.5,reason:'refinement_budget'}];
  assert.throws(()=>validateDataset(data),InputError);
  data.observations=data.observations.map(o=>({...o,use_for_last_seen:false,usable_as_clear_evidence:false,needs_review:true,support_frame_ids:[]}));
  validateDataset(data);
  data.clips[0].unrefined_ranges[0].end=61;
  assert.throws(()=>validateDataset(data),InputError);
});

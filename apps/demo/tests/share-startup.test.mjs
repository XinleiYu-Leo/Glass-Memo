import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createDemoServer } from '../server.mjs';

test('packaged synthetic demo serves search and playback with automatic analysis disabled', async () => {
  const saved=process.env.ROBOMEMO_ENABLE_AUTO;
  delete process.env.ROBOMEMO_ENABLE_AUTO;
  const server=await createDemoServer({directory:fileURLToPath(new URL('../',import.meta.url))});
  try {
    await new Promise((ok,fail)=>server.once('error',fail).listen(0,'127.0.0.1',ok));
    const base=`http://127.0.0.1:${server.address().port}`;
    assert.equal((await fetch(base)).status,200);
    const status=await (await fetch(`${base}/api/analysis/status`)).json();
    assert.equal(status.enabled,false);
    const r=await fetch(`${base}/api/search`,{method:'POST',headers:{Origin:base,'Content-Type':'application/json'},body:JSON.stringify({query:'keys'})});
    assert.equal(r.status,200);
    const answer=(await r.json()).answers[0];
    assert.equal(answer.observation.id,'keys-held');
    const evidence=await fetch(base+answer.observation.evidence_url);
    assert.equal(evidence.status,200);assert.match(evidence.headers.get('content-type'),/image\/png/);
    const media=await fetch(base+answer.observation.video_url,{headers:{Range:'bytes=0-127'}});
    assert.equal(media.status,206);assert.equal((await media.arrayBuffer()).byteLength,128);
    assert.equal((await fetch(`${base}/api/analysis/start`,{method:'POST',headers:{Origin:base,'Content-Type':'application/json'},body:JSON.stringify({clip_id:'synthetic-sequence'})})).status,503);
  } finally {
    if(saved===undefined)delete process.env.ROBOMEMO_ENABLE_AUTO;else process.env.ROBOMEMO_ENABLE_AUTO=saved;
    await new Promise(ok=>server.close(ok));
  }
});

// Starts only an empty, temporary local state; never signs in or contacts a provider.
import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
const freePort=async()=>{
  const s=net.createServer();
  await new Promise((ok,fail)=>s.once('error',fail).listen(0,'127.0.0.1',ok));
  const port=s.address().port;await new Promise(ok=>s.close(ok));return port;
};
test('share bridge initializes a fresh private state and advertises an unconnected localhost service',async()=>{
  const state=await mkdtemp(join(tmpdir(),'robomemo-bridge-empty-test-'));
  const dashboardPort=await freePort(),captionPort=await freePort();
  const child=spawn(process.execPath,[fileURLToPath(new URL('server.mjs',import.meta.url))],{
    env:{...process.env,ROBOMEMO_STATE_DIR:state,ROBOMEMO_DASHBOARD_PORT:String(dashboardPort),ROBOMEMO_CAPTION_PORT:String(captionPort),ROBOMEMO_ALLOW_LAN:'0'},
    stdio:['ignore','pipe','pipe']});
  let output='';child.stdout.on('data',x=>{output+=x});child.stderr.on('data',()=>{});
  try{
    await new Promise((ok,fail)=>{
      const timeout=setTimeout(()=>{clearInterval(timer);fail(new Error('Local bridge startup timed out'));},5000);
      const timer=setInterval(()=>{if(output.includes('iPhone connection:')){clearInterval(timer);clearTimeout(timeout);ok();}},20);
      child.once('error',e=>{clearInterval(timer);clearTimeout(timeout);fail(e)});
    });
    const origin=`http://127.0.0.1:${dashboardPort}`;
    const page=await fetch(origin);assert.equal(page.status,200);
    const cookie=page.headers.get('set-cookie').split(';')[0];
    const result=await (await fetch(origin+'/api/state',{headers:{Cookie:cookie}})).json();
    assert.equal(result.connected,false);assert.equal(result.ready,false);assert.equal(result.account,null);
    assert.equal(result.serverURL,`http://127.0.0.1:${captionPort}`);
    const persisted=JSON.parse(await readFile(join(state,'connection.json'),'utf8'));
    assert.deepEqual(persisted.profiles,[]);assert.equal(persisted.selectedClient,null);
    assert.ok(!Object.hasOwn(persisted,'accessToken'));
  }finally{
    if(child.exitCode===null){const closed=new Promise(ok=>child.once('exit',ok));child.kill('SIGTERM');await closed;}
    await rm(state,{recursive:true,force:true});
  }
});

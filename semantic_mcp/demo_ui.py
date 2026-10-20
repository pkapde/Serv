"""Local test page. All analytical actions use the authenticated MCP transport."""

import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse


def install_demo_ui(app: FastAPI, questions: list[str], prefix: str = "/analytics", resolver_mode="fixture"):
    # Encode untrusted text as JSON and escape HTML script terminators.
    config = json.dumps({"questions": questions, "prefix": prefix}).replace("<", "\\u003c")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def demo_page():
        page = HTML.replace("__DEMO_CONFIG__", config)
        if resolver_mode == "llm":
            page = page.replace(
                "Fixture mode supports the sample questions below. SQL is compiled and executed on local sample data. No LLM credentials are needed.",
                "Live LLM interpretation with synthetic local data. Select an example or type a new analytical question. The provider receives the question and catalogue, not result rows.",
            )
        return page


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CX Analytics MCP Demo</title><style>
:root{font-family:system-ui,sans-serif;color:#172b40;background:#f2f5f9}body{max-width:1060px;margin:40px auto;padding:0 20px}
header{margin-bottom:24px}h1{margin:8px 0}h2{font-size:18px}p{line-height:1.6}.badge{color:#356779;font-size:13px;font-weight:700}
section{background:white;padding:24px;border:1px solid #d8e1eb;border-radius:14px;margin-bottom:18px}
label{display:block;font-weight:600;margin:14px 0 7px}input,select,textarea,button{font:inherit;box-sizing:border-box}
input,select,textarea{width:100%;padding:11px;border:1px solid #b5c3d3;border-radius:7px}textarea{min-height:90px}
button{padding:10px 18px;border:0;border-radius:7px;background:#205b88;color:white;cursor:pointer;margin:12px 8px 0 0}
button:disabled{opacity:.45;cursor:default}.muted{color:#52677b;font-size:14px}#status{white-space:pre-wrap;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:12px;border-bottom:1px solid #e0e6ed}
.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;background:#f5f7fa;padding:14px;border-radius:8px}
details{margin-top:16px}summary{cursor:pointer;font-weight:600}[hidden]{display:none}
</style></head><body>
<header><span class="badge">LOCAL DEMO · SYNTHETIC DATA</span><h1>CX Analytics</h1>
<p>Ask a governed question, inspect its answer, and save it for reuse.</p>
<p class="muted">Fixture mode supports the sample questions below. SQL is compiled and executed on local sample data. No LLM credentials are needed.</p></header>
<section><h2>Connection</h2><label for="token">Bearer token</label>
<input id="token" type="password" autocomplete="off" placeholder="Enter local-demo-token for the default local demo">
<button id="connect">Connect and discover tools</button><p id="connection" class="muted">Not connected</p></section>
<section><h2>Ask a question</h2><label for="samples">Sample question</label><select id="samples"></select>
<label for="question">Question</label><textarea id="question"></textarea><button id="ask">Ask question</button>
<p id="status" role="status" aria-live="polite">Ready. Enter your token, then ask a sample question.</p></section>
<section id="answer" hidden><h2 id="answerTitle">Answer</h2><p id="interpretation"></p>
<div class="scroll"><table id="results"></table></div><p id="truncated" class="muted"></p>
<details open><summary>Provenance and choices</summary><pre id="provenance"></pre></details>
<details><summary>Full response</summary><pre id="raw"></pre></details></section>
<section><h2>Save and rerun</h2><label for="saveName">Query name</label><input id="saveName" value="My CX question">
<button id="save" disabled>Save answered question</button><label for="savedId">Saved query ID</label>
<input id="savedId" placeholder="Save a question or paste an existing ID"><button id="rerun">Run saved query</button>
<p class="muted">Saved runs use REST and skip the resolver LLM. Tokens are kept only in this page's memory.</p></section>
<script>
'use strict';
const config=__DEMO_CONFIG__, $=id=>document.getElementById(id);
let rpcId=0,connectedToken=null,resolutionId=null,busy=false;
const controls=['connect','ask','save','rerun'];
for(const q of config.questions){const o=document.createElement('option');o.textContent=q;o.value=q;$('samples').append(o);}
$('question').value=$('samples').value;
$('samples').onchange=()=>{$('question').value=$('samples').value;};
function token(){const value=$('token').value.trim();if(!value)throw Error('Enter your bearer token first.');return value;}
async function request(path,body){
 const response=await fetch(path,{method:'POST',headers:{'Authorization':'Bearer '+token(),'Content-Type':'application/json','Accept':'application/json, text/event-stream'},body:JSON.stringify(body)});
 const raw=await response.text();let payload;try{payload=raw?JSON.parse(raw):{};}catch{throw Error('Unexpected server response (HTTP '+response.status+').');}
 if(!response.ok)throw Error(payload.message||payload.detail||('HTTP '+response.status));return payload;
}
async function rpc(method,params={},notification=false){
 const body={jsonrpc:'2.0',method,params};if(!notification)body.id=++rpcId;
 const data=await request(config.prefix+'/mcp',body);if(data.error)throw Error(data.error.message);return data.result;
}
async function connect(){
 const active=token();
 await rpc('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'cx-browser-demo',version:'1.0'}});
 await rpc('notifications/initialized',{},true);
 const result=await rpc('tools/list');const names=result.tools.map(t=>t.name);
 if(!names.includes('ask_question')||!names.includes('save_query'))throw Error('Required MCP tools are missing.');
 connectedToken=active;$('connection').textContent='Connected · '+names.join(', ');
}
async function tool(name,args){
 if(connectedToken!==token())await connect();
 const result=await rpc('tools/call',{name,arguments:args});
 if(result.isError)throw Error((result.content||[]).map(c=>c.text||'').join('\n'));
 if(!result.structuredContent)throw Error('The MCP tool returned no structured response.');return result.structuredContent;
}
function show(data){
 $('answer').hidden=false;$('answerTitle').textContent=data.status==='answered'?'Answer':data.status;
 $('interpretation').textContent=data.interpretation||data.message||'';
 $('provenance').textContent=JSON.stringify(data.provenance||{},null,2);$('raw').textContent=JSON.stringify(data,null,2);
 const table=$('results');table.replaceChildren();
 if(data.columns?.length){const row=document.createElement('tr');for(const c of data.columns){const th=document.createElement('th');th.textContent=c.name;row.append(th);}table.append(row);}
 for(const values of data.rows||[]){const row=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value===null?'null':String(value);row.append(td);}table.append(row);}
 $('truncated').textContent=data.truncated?'Results truncated at the configured limit.':'';
 resolutionId=data.status==='answered'?data.resolution_id:null;
 $('status').textContent=data.status==='answered'?'Answered · '+(data.rows||[]).length+' rows':(data.message||data.status);
 if(data.candidates?.length)$('status').textContent+='\nCandidates: '+data.candidates.map(c=>typeof c==='string'?c:JSON.stringify(c)).join(', ');
}
async function action(fn){if(busy)return;busy=true;controls.forEach(id=>$(id).disabled=true);$('status').textContent='Working…';
 try{await fn();}catch(error){$('status').textContent='Error: '+error.message;}finally{busy=false;controls.forEach(id=>$(id).disabled=false);$('save').disabled=!resolutionId;}}
$('connect').onclick=()=>action(async()=>{await connect();$('status').textContent='Connection verified. Ask a sample question.';});
$('ask').onclick=()=>action(async()=>{resolutionId=null;show(await tool('ask_question',{question:$('question').value}));});
$('save').onclick=()=>action(async()=>{const data=await tool('save_query',{resolution_id:resolutionId,name:$('saveName').value});
 if(data.status!=='answered')throw Error(data.message||'Save failed');$('savedId').value=data.saved_query_id;$('status').textContent='Saved · '+data.saved_query_id;});
$('rerun').onclick=()=>action(async()=>{const id=$('savedId').value.trim();if(!id)throw Error('Save a question or enter a saved query ID first.');
 show(await request(config.prefix+'/api/v1/saved-queries/'+encodeURIComponent(id)+'/run',{}));});
</script></body></html>"""

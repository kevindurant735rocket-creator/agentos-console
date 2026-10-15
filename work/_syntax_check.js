
"use strict";
const D = (()=>{ try{ return JSON.parse(document.getElementById('payload').textContent); }
  catch(e){ return {__err:String(e)}; } })();
const $ = s=>document.querySelector(s);
const esc = s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cls = v=>['PASS','PARTIAL','FAIL'].includes(v)?v:'NONE';
// D48 线性朗读：1503 字证据原文整段塞进一个 text 节点时，读屏只能一口气念到底，
// 用户既停不下来也跳不过去。按句读边界切块，每块独立成节点（视觉不变，仍连排）。
const CHUNK_MAX = 160;
function chunk(s,max){
  s=String(s==null?'':s); max=max||CHUNK_MAX;
  if(s.length<=max) return [s];
  const seps=['，','；','。',',',';',' ','\n']; const out=[]; let rest=s;
  while(rest.length>max){
    let cut=-1;
    for(const sp of seps){ const i=rest.lastIndexOf(sp,max); if(i>max*0.5){ cut=i+sp.length; break; } }
    if(cut<0) cut=max;
    out.push(rest.slice(0,cut)); rest=rest.slice(cut);
  }
  if(rest) out.push(rest);
  return out;
}
function chunkHTML(s,max,cls){
  return chunk(s,max).map(c=>'<span class="ln">'+esc(c)+'</span>').join('');
}

/* ── 四态 ───────────────────────────────── */
// 加载态：极短展示（内联数据），随后进入正常渲染
function showState(kind, cfg){
  const box=$('#stBox'); const S=$('#states'); S.classList.add('on');
  const T={
    loading:['<span class="spin"></span> 正在解析内联载荷…','读取 data.json 内联块，构建阶段矩阵。'],
    empty:['没有可显示的阶段数据','载荷里 dirs 数组为空或缺失。','通常意味着 verify_all 尚未产出 verdicts.json。'],
    error:['载荷解析失败','内联 JSON 无法解析，页面无法构建矩阵。'],
    notfound:['404 · 视图不存在','请求的视图 id 在本构建中不存在。']
  }[kind]||['未知状态',''];
  box.innerHTML='<h3>'+T[0]+'</h3><p>'+T[1]+'</p>'+
    (T[2]?'<p>'+T[2]+'</p>':'')+
    (cfg&&cfg.detail?'<code>'+esc(cfg.detail)+'</code>':'')+
    '<div class="act">'+(kind==='error'||kind==='empty'||kind==='notfound'
      ?'<button class="btn pri" onclick="location.reload()">重新加载</button>'+
       '<button class="btn" onclick="document.getElementById(\'states\').classList.remove(\'on\')">返回</button>'
      :'')+'</div>';
  if(kind==='loading') box.insertAdjacentHTML('beforeend',
    '<div class="sk"><i></i><i></i><i></i><i></i></div>');
}

/* ── 渲染 ───────────────────────────────── */
let sel=-1;
/* R5 第 6 视图 Agent 台。CL/TAB 是 view() 的唯一真源，加视图必须同改这两张表
   + 命令面板 VIEWS + 数字键映射，漏一处就会出现「tab 存在但切不过去」的半接线。
   D95：TAB 的**键序就是数字键序**（2376 行 `Object.keys(TAB)[+k-1]`），所以这里
   的插入位置必须与 tablist 里按钮的视觉顺序逐字一致。能力地图放在复盘台之后，
   工作区顺延到 10 —— 两个「学习面」相邻，而不是为了保住旧编号把新面塞到末尾。 */
const CL={over:'v-over',round:'v-round',mx:'v-mx',led:'v-led',eng:'v-eng',work:'v-work',
  goal:'v-goal',retro:'v-retro',cap:'v-cap',ws:'v-ws',
  term:'v-term',file:'v-file',set:'v-set'};   // R7 新视图三面（终端/文件/设置）同源接线
const TAB={over:'t-over',round:'t-round',mx:'t-mx',led:'t-led',eng:'t-eng',work:'t-work',
  goal:'t-goal',retro:'t-retro',cap:'t-cap',ws:'t-ws',
  term:'t-term',file:'t-file',set:'t-set'};
const TABMAX=Object.keys(TAB).length;   // 数字键上限从 TAB 推导，不写死（原写死 '7'，加第 8 视图必失配）

function fmtT(s){ if(!s) return '—';
  const d=new Date(s); if(isNaN(d)) return String(s);
  return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function dur(d){ return (d==null)?'—':(Number(d).toFixed(1)+'s'); }

/* ── D41 证据时效 ─────────────────────────────
   阈值集中在此，UI 别处一律不许再写魔法数。
   fail-closed：generated_at 缺失或不可解析 -> 判「未知」，绝不判新鲜。 */
const FRESH_MIN=30, FRESH_MAX=360;   // 分钟：<=30 新鲜，<=360 琥珀，>360 陈旧
const FRESH_TICK_MS=30000;           // 徽章自刷新周期（见 freshTick）
function evidenceAge(){
  const raw=(D&&D.generated_at)||null;
  const t=raw?Date.parse(raw):NaN;
  if(!raw||isNaN(t)){
    return {f:'unknown',ms:null,label:'时间未知',at:null,
      why:'data.json 缺 generated_at 或无法解析 —— 按 fail-closed 判为不可信新鲜度'};
  }
  const ms=Date.now()-t;
  const f=ms<=FRESH_MIN*60000?'fresh':(ms<=FRESH_MAX*60000?'amber':'stale');
  const h=ms/3600000;
  const label=ms<3600000?(Math.max(1,Math.round(ms/60000))+' 分钟前')
    :(h<48?(h.toFixed(1)+' 小时前'):(Math.round(h/24)+' 天前'));
  return {f:f,ms:ms,label:label,at:raw,why:''};
}
const FV={fresh:'新鲜',amber:'偏旧',stale:'陈旧',unknown:'未知'};
function freshness(){
  const a=evidenceAge();
  $('#fresh').innerHTML='<span class="fresh" data-f="'+a.f+'" title="证据产生于 '
    +esc(a.at||'未知')+'；这是历史结论，不是当前实测">证据 '+esc(FV[a.f])
    +' · '+esc(a.label)+'</span>';
  const b=$('#sbn');
  if(a.f==='fresh'){ b.classList.remove('on'); return a; }
  b.classList.add('on');
  $('#sbn1').textContent=(a.f==='unknown')
    ?'证据新鲜度不可判定（fail-closed）'
    :('证据已 '+a.label+'未复跑');
  $('#sbn2').innerHTML='顶栏 <b>PASS</b> 是 '+esc(fmtT(D.generated_at))
    +' 的<b>历史结论</b>，不代表当前真树状态。'
    +(a.f==='unknown'?esc(a.why):'')
    +' 证据源 <b>'+esc(D.evidence_source||'未记录')+'</b>';
  return a;
}
/* 底栏那一格要单独更新，所以给独立 id —— 不重建 strip()，免得把整条底栏
   连同里面的节点一起换掉（换掉会丢焦点、也会让截图对不上）。 */
function freshnessFoot(){
  const a=evidenceAge(); const el=$('#ftfresh'); if(!el) return a;
  el.className=(a.f==='fresh'?'gateok':'gatebad');
  el.textContent=FV[a.f]+' '+a.label;
  return a;
}
/* D41 续：徽章原来只在 boot() 算一次 —— 页面一直开着，它会永远显示「新鲜」。
   实测取证（26h 假时钟）：evidenceAge() 已判 stale，DOM 仍是 fresh、横幅 none。
   操作员盯一屏就是一下午，只有轮询刷新才能让徽章随时间真的翻面。 */
function freshTick(){ freshness(); freshnessFoot(); verdictTag(); }

/* ── D86：顶栏 verdict 徽章必须自己带上时效限定词 ─────────────────────
   真缺陷（看真渲染截图抓的，27 门全绿也盖不住）：顶栏那颗最显眼的绿色
   「PASS」只读 D.global_verdict，既不看证据时效，也跟 freshTick() 无关。
   于是一个 3 天前验出来的 PASS，顶栏仍是纯绿无限定词，而「陈旧」两个字
   只出现在 200px 外的红横幅里 —— 视觉权重完全压不住，用户第一眼必然误判
   「现在是好的」。这正是本仓 doctrine 要堵的「量具绿但量到的是历史」。
   两处修：① 徽章自身随证据时效翻面，不再依赖横幅替它说话；
            ② 进 freshTick()，页面开着也能随时间真的翻面（同 D41 徽章老账）。
   注意不把 PASS 改写成 PARTIAL —— 那是在另一个方向撒谎。verdict 仍是 PASS，
   只是降级成 HIST 灰并挂上时效限定词，读者一眼看到「PASS，但是陈旧的」。 */
function verdictTag(){
  const el=$('#vtag'); if(!el) return;
  const gv=String(D.global_verdict);
  const a=evidenceAge();
  if(a.f==='fresh'){
    el.innerHTML='<span class="vtag '+cls(gv)+'">'+esc(gv)+'</span>';
  }else{
    /* 只带状态词，不带年龄：年龄就在左邻的 #fresh 徽章上写着
       （「证据 陈旧 · 3 天前」），这里再写一遍是纯冗余，还把顶栏挤爆 ——
       实测 768 下顶栏超预算 63px，.stamp 被压到 31px、分隔线压到 0。
       完整年龄仍在 title 里，悬停可读。诚实性不减：徽章自己讲清「已陈旧」。 */
    const q=(a.f==='unknown')?(' · 时效未知'):(' · '+FV[a.f]);
    el.innerHTML='<span class="vtag HIST" title="'+esc(gv)+' 是 '+esc(a.label)
      +'的历史结论，不代表当前真树状态">'+esc(gv)+' '+esc(q)+'</span>';
  }
}

function view(key){
  Object.keys(CL).forEach(k=>{
    $('#'+CL[k]).classList.toggle('on',k===key);
    $('#'+TAB[k]).setAttribute('aria-selected',String(k===key));
  });
  /* ARIA APG 的 tab 模式是「roving tabindex」：整个 tablist 在 Tab 序列里只占一个停靠点，
     组内移动交给方向键。原来 5 个 tab 全是默认 tabindex=0，读屏/键盘用户要按 5 次 Tab
     才能换组，且左右方向键无效 —— 实测 tools/a11y_walk.py 抓到过这条红。 */
  Object.keys(TAB).forEach(k=>$('#'+TAB[k]).setAttribute('tabindex',k===key?'0':'-1'));
  if(key==='round'&&sel<0) sel=0;
  if(key==='work') wkHealth();          // 进 Agent 台即刷新真指纹，别拿上一轮快照充数
  if(key==='goal'){ goalLoad(); chainLoad(); preflightReset(); }   // 不预填任何假计划/假台账/假就绪度，空就是空
  if(key==='retro'){ retroReset(); }    // 同上：复盘台不预填假账本，读不到就是读不到
  if(key==='ws'&&!wsPath) wsList('');    // 工作区第一次进来才拉目录，不预填任何假条目
  if(key==='cap'){ capReset(); }      // 能力地图不预填任何假账：进来就是「未读」
  if(key==='term'&&!tinited) termInit();   // R7 新面：终端/文件/设置惰性初始化（与 ws 同模式）
  if(key==='file'&&!finited) fileInit();
  if(key==='set'&&!sinited) setInit();
  $('#main').scrollTop=0;
}

/* tablist 方向键导航（APG）：←/→ 移动并激活，Home/End 跳首尾，↑/↓ 交给矩阵自身用 */
function tabMove(e){
  const ks=Object.keys(TAB), cur=ks.indexOf(e.target.id.replace(/^t-/,''));
  if(cur<0) return;
  let to=null;
  if(e.key==='ArrowRight') to=ks[(cur+1)%ks.length];
  else if(e.key==='ArrowLeft') to=ks[(cur-1+ks.length)%ks.length];
  else if(e.key==='Home') to=ks[0];
  else if(e.key==='End') to=ks[ks.length-1];
  if(!to) return;
  e.preventDefault();
  view(to);
  $('#'+TAB[to]).focus();
}

/* 脊线 */
function rail(){
  const rs=D.rounds||[];
  $('#rail').innerHTML = rs.length? rs.map((r,i)=>{
    const st=cls(r.status&&/PARTIAL|FAIL/.test(r.status)?(r.status.includes('FAIL')?'FAIL':'PARTIAL'):'PASS');
    return '<button class="node" data-st="'+st+'" data-i="'+i+'" '+
      'aria-current="'+(i===0?'true':'false')+'" '+
      'title="'+esc(r.goal)+'"><span class="r">'+esc(r.round)+'</span>'+
      '<span class="s">'+esc((r.status||'')+' · '+esc(r.ac||''))+'</span></button>';
  }).join('') : '<p class="spfoot">GOALS.md 未解析出轮次行。</p>';
  $('#spfoot').innerHTML=
    '<div class="sl" role="group" aria-label="轮次账"><span class="k">轮次账</span>'+
    rs.map(r=>'<div class="r"><b>'+esc(r.round)+'</b><span>'+esc(r.run||'—')+'</span></div>').join('')+
    '</div>'+
    '<div class="sl" role="group" aria-label="口径分歧速查"><span class="k">口径分歧速查</span>'+
    ((D.divergence||[]).length
      ? (D.divergence).map(d=>'<div class="dq">'+
          '<span class="dot '+cls(d.declared)+'"></span>'+esc(d.dir.replace(/^phase/,''))+
          '<span class="a">'+esc(d.declared)+'→'+esc(d.gate)+'</span></div>').join('')
      : '<div class="r"><span>无</span></div>')+
    '</div>'+
    '<div class="sl lg"><span class="k">图例</span>'+
    '<div class="r"><span><i class="sw PASS"></i>PASS</span><i class="sw PARTIAL"></i>PARTIAL'+
    '<i class="sw FAIL"></i>FAIL</span></div></div>';
  $('#rail').querySelectorAll('.node').forEach(b=>
    b.addEventListener('click',()=>{ pick(+b.dataset.i); }));
}
// 热重载：仅本地 http 协议下启用；file:// 直开时保持 0 外部请求
(function(){
  var el=document.getElementById('live'); if(!el) return;
  if(location.protocol.indexOf('http')!==0){ el.textContent='静态'; return; }
  // D41：这里只能证明「连接活着」，不能证明「数据新鲜」。
  // 新鲜度由独立的「证据 N 小时前」徽章负责，两者不再共用一个 LIVE 字样。
  el.title='仅表示与本地服务的连接状态；数据新鲜度见顶栏「证据」徽章';
  var set=function(t,s){ el.textContent=t; if(s) el.setAttribute('data-s',s); };
  var es=new EventSource('__watch');
  es.addEventListener('hello',function(){ set('已连接','ok'); });
  es.addEventListener('reload',function(){ set('重载中','ok'); wkEv('reload'); location.reload(); });
  es.addEventListener('build-failed',function(){ set('构建失败','err'); });
  es.addEventListener('extract-failed',function(){ set('抽取失败','err'); wkEv('extract-failed'); });
  es.addEventListener('build-ok',function(){ wkEv('build-ok'); });
  es.onerror=function(){ set('断开','err'); };
})();
function pick(i){
  sel=i;
  $('#rail').querySelectorAll('.node').forEach((b,j)=>
    b.setAttribute('aria-current',String(j===i)));
  const d=document.querySelectorAll('#rounds .rd'); if(d[i]) d[i].open=true;
  view('round'); $('#rounds').scrollIntoView({block:'nearest'});
}

/* 分歧横幅 —— 诚实核心 */
function divergence(){
  const n=D.n_divergence||0; const box=$('#divbox');
  if(!n){ box.innerHTML=''; return; }
  const items=(D.divergence||[]).map(d=>
    '<div class="dv"><div class="h"><span class="d">'+esc(d.dir)+'</span>'+
    '<span class="vtag sm '+cls(d.declared)+'">自报 '+esc(d.declared)+'</span>'+
    '<span class="arw">→</span>'+
    '<span class="vtag sm '+cls(d.gate)+'">实测 '+esc(d.gate)+'</span>'+
    '<span class="pill">'+esc(d.adjudication)+'</span></div>'+
    (d.sample&&d.sample.length?'<ul>'+d.sample.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ul>':'')+
    '</div>').join('');
  box.innerHTML='<details class="div" open><summary><span class="t">口径分歧 '+n+' 处</span>'+
    '<span class="msg">总闸 <b>'+esc(D.global_verdict)+'</b> 建立在实测口径上；'+
    '其中 <b>'+(D.divergence||[]).filter(d=>d.adjudication==='需人工裁决').length+
    '</b> 处自报口径与实测不一致，<b>未自动合并</b>。</span></summary>'+
    '<div class="divb">'+items+'</div></details>';
}

/* 小 stat 条 */
function stats(){
  const a=D.aggregate||{}; const dc=D.declared_counts||{}; const gc=D.gate_counts||{};
  const m=D.manifest||{};
  const S=[
    ['阶段目录',(a.n_dirs||0),'',esc((D.coverage&&D.coverage.expected_dirs?D.coverage.expected_dirs.length:0))+' 期望目录'],
    ['实测 PASS',gc.PASS||0,'pass','闸门口径'],
    ['自报 FAIL',dc.FAIL||0,dc.FAIL?'fail':'idle','与实测差 '+(D.n_divergence||0)+' 处'],
    ['实测 PARTIAL',gc.PARTIAL||0,'partial',''],
    ['已执行',a.N_EXECUTED||0,'gate','/ '+(a.n_dirs||0)],
    ['manifest',(m.present||0)+'/'+(m.n_total||0),(m.missing?'partial':'pass'),m.missing?(m.missing+' 个缺失'):'全覆盖'],
    ['环境降级',(D.env_gated||[]).length,(D.env_gated||[]).length?'partial':'idle',''],
    ['自报/实测分歧',D.n_divergence||0,(D.n_divergence?'partial':'idle'),'需人工裁决']
  ];
  $('#stats').innerHTML=S.map((s,i)=>'<div class="st1" role="listitem" aria-label="'+
    esc(s[0])+'：'+esc(s[1])+(s[3]?'，'+esc(s[3]):'')+'">'+
    '<div class="k" aria-hidden="true">'+esc(s[0])+'</div>'+
    '<div class="v '+(s[2]||'')+'" aria-hidden="true">'+esc(s[1])+'</div>'+
    (s[3]?'<div class="n" aria-hidden="true">'+esc(s[3])+'</div>':'')+'</div>').join('');
  $('#ovmeta').textContent='· '+esc(fmtT(D.generated_at));
}

function ovgate(){
  const a=D.aggregate||{}; const m=D.manifest||{};
  const indep=D.independent||{};
  const rows=[
    ['全局结论',String(D.global_verdict)+'（覆盖率 '+(D.coverage_complete?'齐':'不齐')+'）',''],
    ['实测计数','PASS '+(D.gate_counts&&D.gate_counts.PASS||0)+
      ' / PARTIAL '+(D.gate_counts&&D.gate_counts.PARTIAL||0)+
      ' / FAIL '+(D.gate_counts&&D.gate_counts.FAIL||0),''],
    ['自报计数','PASS '+(D.declared_counts&&D.declared_counts.PASS||0)+
      ' / PARTIAL '+(D.declared_counts&&D.declared_counts.PARTIAL||0)+
      ' / FAIL '+(D.declared_counts&&D.declared_counts.FAIL||0),'与实测不同，见口径分歧'],
    ['闸门内部','AGREE '+(a.AGREE||0)+' · GATE_SELF '+(a.GATE_SELF||0)+
      ' · TIMEOUT '+(a.TIMEOUT||0)+' · ORPHAN_SHELL '+(a.ORPHAN_SHELL||0)+
      ' · HONESTY_VIOLATION '+(a.HONESTY_VIOLATION||0),''],
    ['诚实性违规',String(a.HONESTY_VIOLATION||0),(a.HONESTY_VIOLATION?'fail':'pass')],
    ['自报绿但实测死',String(a.SELF_REPORT_PASS_BUT_RUN_FAILED||0),
      (a.SELF_REPORT_PASS_BUT_RUN_FAILED?'fail':'pass')],
    ['manifest 覆盖',(m.present||0)+'/'+(m.n_total||0),m.missing?'partial':'pass'],
    // D55：两个「分歧」口径并列，不合并（extract.py docstring 纪律）：
    //   independent.n_mismatches = 独立审计器重算 vs 闸门结果（上游字段）
    //   D.n_divergence          = 阶段自报 vs 闸门实测（本项目抽取）
    // 原来两处都只写「分歧 N」，操作员会以为它们是同一个数（实测 0 与 3 并排出现，
    // 看起来像自相矛盾）。现在各带口径名。
    ['独立审计',(indep.verdict==null?'未给结论（null）':String(indep.verdict))+
      ' · 重算一致 '+(indep.consistent==null?'未记录':(indep.consistent?'是':'否'))+
      ' · 审计器分歧 '+(indep.n_mismatches==null?'?':indep.n_mismatches),
      indep.verdict==null?'fail':(indep.consistent===false?'partial':'pass')]
  ];
  $('#ovgate').innerHTML=rows.map(r=>'<div class="e1"><div class="k">'+esc(r[0])+
    '</div><div class="v '+(r[2]||'')+'">'+esc(r[1])+'</div></div>').join('');
}

/* 轮次 */
function rounds(){
  const rs=D.rounds||[];
  $('#rdcount').textContent='· '+rs.length+' 轮（逐轮展开）';
  $('#rounds').innerHTML=rs.map((r,i)=>{
    const st=cls(/FAIL/.test(r.status||'')?'FAIL':/PARTIAL/.test(r.status||'')?'PARTIAL':'PASS');
    return '<details class="rd" data-i="'+i+'"><summary><span class="tag">'+esc(r.round)+
      '</span><span class="g">'+esc(r.goal)+'</span>'+
      '<span class="st vtag '+st+'">'+esc(r.status||'')+'</span></summary>'+
      '<div class="rdb">'+
      '<div class="lbl">验收判据</div><p>'+esc(r.criteria)+'</p>'+
      '<div class="lbl">本轮增量</div><p>'+esc(r.delta)+'</p>'+
      '<div class="lbl">运行账 '+esc(r.run||'')+' · AC '+esc(r.ac||'')+'</div>'+
      '<div class="lbl">证据</div><div class="mono" role="group" aria-label="'+
      esc(r.round)+' 证据原文，共 '+String(r.evidence||'').length+' 字">'+chunkHTML(r.evidence)+'</div>'+
      '</div></details>';
  }).join('')||'<p class="note">GOALS.md 未解析出轮次行。</p>';
}

/* 矩阵 */
function matrix(){
  const ds=D.dirs||[];
  $('#mxct').textContent='· '+ds.length+' 阶段 · 点选查看 · 分歧格右上角有琥珀点';
  $('#mx').innerHTML=ds.map((d,i)=>
    '<button class="cell'+(d.agree?'':' diverged')+'" data-i="'+i+'" aria-pressed="false" '+
    'aria-label="'+esc(d.dir)+' 实测 '+esc(d.gate)+' 自报 '+esc(d.declared)+
    (d.agree?'':' 口径分歧')+'"><span class="n">'+esc(d.dir.replace(/^phase/,''))+
    '</span><span class="b"><span class="dot '+cls(d.gate)+'"></span>'+
    '<span class="g">'+esc(d.gate||'—')+'</span></span></button>').join('');
  const cells=[...$('#mx').querySelectorAll('.cell')];
  cells.forEach(c=>c.addEventListener('click',()=>readout(+c.dataset.i)));
  $('#mx').addEventListener('keydown',e=>{
    const i=cells.indexOf(document.activeElement); if(i<0) return;
    let j=-1;
    if(e.key==='ArrowRight') j=i+1; else if(e.key==='ArrowLeft') j=i-1;
    else if(e.key==='ArrowDown') j=i+9; else if(e.key==='ArrowUp') j=i-9;
    else if(e.key==='Home') j=0; else if(e.key==='End') j=cells.length-1;
    else if(e.key==='Enter'||e.key===' '){ e.preventDefault(); readout(i); return; }
    else return;
    e.preventDefault();
    if(j>=0&&j<cells.length){ cells[j].focus(); readout(j); }
  });
  if(cells.length) readout(0);
}
function readout(i){
  const d=(D.dirs||[])[i]; if(!d) return;
  sel=i;
  $('#mx').querySelectorAll('.cell').forEach((c,j)=>
    c.setAttribute('aria-pressed',String(j===i)));
  const man=d.manifest||{};
  const vc=man.verdict_counts||{};
  $('#ro').innerHTML='<div class="h"><span class="d">'+esc(d.dir)+'</span>'+
    '<span class="vtag '+cls(d.gate)+'">实测 '+esc(d.gate)+'</span>'+
    '<span class="vtag '+cls(d.declared)+'">自报 '+esc(d.declared)+'</span>'+
    (d.agree?'':'<span class="vtag PARTIAL">口径分歧</span>')+'</div>'+
    '<dl><dt>exit</dt><dd>'+esc(d.exit==null?'null':d.exit)+'</dd>'+
    '<dt>耗时/预算</dt><dd>'+dur(d.dur)+' / '+(d.budget==null?'—':d.budget+'s')+'</dd>'+
    '<dt>失败条目</dt><dd>'+d.fail_total+' 条 · 负控 '+d.fail_negctrl+
      ' · <span class="'+(d.fail_real?'c-partial':'c-pass')+'">真红 '+d.fail_real+'</span></dd>'+
    '<dt>manifest</dt><dd>'+(man.present
        ? ('在 · measured '+(man.measured_at||'?')+(Object.keys(vc).length?(' · '+JSON.stringify(vc)):''))
        : '缺失')+'</dd>'+
    (d.labels&&d.labels.length?('<dt>labels</dt><dd>'+esc(d.labels.join(', '))+'</dd>'):'')+
    '</dl>'+
    (d.reasons&&d.reasons.length?('<div class="lbl sub">reasons</div><ul>'+
      d.reasons.map(r=>'<li>'+esc(r)+'</li>').join('')+'</ul>'):'')+
    '<p class="note">负控=对抗性注入用例(fake-inject / mutation)，设计上应当失败，不计入缺陷。'+
    '「真红」= 排除负控后仍失败的条目。</p>';
}

/* 账本 */
function ledger(){
  const ds=D.dirs||[];
  const divSet=new Set((D.divergence||[]).map(d=>d.dir));
  $('#ledct').textContent='· '+ds.length+' 行 · 分歧行以琥珀标注';
  $('#led').innerHTML=ds.map(d=>
    '<tr><td class="d">'+esc(d.dir)+'</td>'+
    '<td class="'+(d.gate==='PASS'?'ok':d.gate==='PARTIAL'?'wa':'no')+'">'+esc(d.gate||'—')+'</td>'+
    '<td class="'+(d.declared==='PASS'?'ok':d.declared==='PARTIAL'?'wa':'no')+'">'+
      esc(d.declared||'—')+(divSet.has(d.dir)?' ◆':'')+'</td>'+
    '<td>'+esc(d.exit==null?'null':d.exit)+'</td>'+
    '<td>'+dur(d.dur)+'/'+(d.budget==null?'—':d.budget+'s')+'</td>'+
    '<td>'+d.fail_total+' (负'+d.fail_negctrl+' 红'+d.fail_real+')</td>'+
    '<td>'+(d.manifest&&d.manifest.present?'在':'缺')+'</td></tr>').join('');
  $('#lednote').innerHTML='◆ = 自报口径与实测口径不一致（'+divSet.size+' 处），'+
    '见总览「口径分歧」。负控=设计上应当失败的对抗性用例，不计入真红。';
}

/* 引擎 */
function engine(){
  const a=D.aggregate||{}; const ia=D.interpreter_assert||{};
  const indep=D.independent||{};
  const rows=[
    ['载荷生成时间',fmtT(D.generated_at)],
    ['全局结论',D.global_verdict],
    ['覆盖率完整',D.coverage_complete?'是':'否'],
    ['期望目录数',(D.coverage&&D.coverage.expected_dirs||[]).length],
    ['额外目录数',(D.coverage&&(D.coverage.extra_dirs||[]).length)||0],
    ['解释器断言',(ia.ok===false?'FAIL':(ia.ok?'OK':'未记录'))+
      ((ia.missing_modules&&ia.missing_modules.length)?(' · 缺 '+ia.missing_modules.join(',')):'')],
    ['实测 Python',D.python_actual||'未记录'],
    ['独立审计结论',indep.verdict==null?'null（审计未给结论）':indep.verdict],
    ['审计器重算一致',indep.consistent==null?'未记录':(indep.consistent?'是':'否')],
    ['审计器分歧（重算 vs 闸门）',indep.n_mismatches==null?'?':indep.n_mismatches],
    ['自报-实测分歧（阶段自报 vs 闸门）',D.n_divergence==null?'?':D.n_divergence],
    ['AGREE 桶',a.AGREE],['TIMEOUT 桶',a.TIMEOUT],
    ['ORPHAN_SHELL 桶',a.ORPHAN_SHELL],['NOT_EXECUTED 桶',a.NOT_EXECUTED],
    ['MANIFEST_INVALID 桶',a.MANIFEST_INVALID]
  ];
  $('#engct').textContent='· 全部字段来自 verdicts.json';
  $('#eng').innerHTML=rows.map(r=>'<div class="e1"><div class="k">'+esc(r[0])+
    '</div><div class="v">'+esc(r[1]==null?'null':r[1])+'</div></div>').join('');
  $('#rule').innerHTML=chunkHTML(D.rule||'（verdicts.json 未带 rule 字段）');
  const eg=D.env_gated||[];
  $('#envg').innerHTML=eg.length?eg.map(e=>'<div class="e1"><div class="k">'+esc(e.dir)+
    ' · '+esc(e.declared_verdict)+'</div><div class="v">'+
    chunkHTML((e.evidence||[]).slice(0,2).join(' · '))+'</div></div>').join('')
    :'<div class="e1"><div class="k">env_gated</div><div class="v">空</div></div>';
}

/* 底栏 */
/* 执行台：把「观察→决策→执行→验证」闭环里缺的执行一环接上。
   动作目录向服务端要（allowlist 单源），拿不到就用本地兜底表，
   这样 file:// 直开或服务未起时台面不至于空白。 */
const ACT_FALLBACK=[['rebuild','重抽真树 + 重建驾驶舱'],
  ['snapshot','刷闭环三面快照（直开兜底）'],['verify','跑视觉/无障碍/UX 三门'],
  ['egate','企业门（真树口径）'],['livecheck','上线自证 __LIVECHECK_N__ 项'],
  ['desktop','交付物三件套门'],['evolution','跨 run 复发晋升门'],
  ['promote','可执行晋升门'],['doc-drift','文档数字零漂移']];
let ACTS=ACT_FALLBACK, actBusy=false;
/* ── R5 Agent 台 ─────────────────────────────────────────
   五面全部接真数据：工具卡片=allowlist 真表、审批=显式授权态、
   transcript=/__act/stream SSE、diff=/__health 指纹 + /__watch 事件、
   composer=斜杠命令映射 allowlist。拿不到真数据一律显式报「不可判定」，
   绝不画占位空壳冒充已完成（与驾驶舱诚实口径一致）。 */
const WKPEND=new Set();          // 已授权待执行的动作
const WKSEEN=new Map();          // action -> 最近一次真实 rc/dur
const WKHI=[];                   // hint 历史（带时序）：中止与回执两条异步流会赛跑，
                                 // 光看当前这一句分不清「谁最后写的」，门要判的是顺序。
let WKFP=null, WKWATCH=null, WKPOLL=null, WKREC=[], WKEV=[];

function wkhint(txt,kind){
  const el=$('#wkhint'); if(!el) return;
  WKHI.push({t:Date.now(),txt:String(txt||'')});
  if(WKHI.length>60) WKHI.shift();
  el.innerHTML=txt?('<span class="'+(kind||'')+'">'+esc(txt)+'</span>'):'';
}
function wklog(txt,kind){
  const box=$('#wktrbox'); if(!box) return;
  const idle=box.querySelector('.idle'); if(idle) idle.remove();
  const d=document.createElement('div');
  d.className='m '+(kind||'');
  d.textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false})+'  '+txt;
  box.appendChild(d);
  while(box.children.length>200) box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}
/* __watch 事件记账：复用热重载那条 SSE（不额外开连接），供 Agent 台 diff 面读 */
function wkEv(name){
  WKEV.push({t:new Date().toLocaleTimeString('zh-CN',{hour12:false}),name:String(name)});
  if(WKEV.length>60) WKEV.shift();
  const box=$('#wkdfbox');
  if(box&&$('#v-work')&&$('#v-work').classList.contains('on')) wkDiff(WKFP,WKFP);
}
/* 工具调用卡片：名字/描述取服务端 allowlist 真表，rc/dur 只写真回执 */
function wkTools(){
  const box=$('#wktools'); if(!box) return;
  box.innerHTML=ACTS.map(a=>{
    const r=WKSEEN.get(a[0]);
    const rc=r?(' <span class="rc '+(r.rc===0?'ok':'no')+'">rc='+esc(r.rc)
      +'</span> <span>'+esc(r.dur)+'s</span>'):'';
    return '<div class="a1"><div class="n">'+esc(a[0])+rc+'</div>'+
      '<div class="d">'+esc(a[1])+'</div>'+
      '<button class="abtn" data-wk="'+esc(a[0])+'"'+(actBusy?' disabled':'')+
      ' aria-label="运行 '+esc(a[0])+'：'+esc(a[1])+'">运行</button></div>';
  }).join('');
}
/* 审批卡：授权是显式状态。服务端 POST /__act 要求 X-AgentOS-Action:1，
   台面把这一步显式画成「授权」——授权前不许发请求，授权后才带头发。
   why 一律取 recommend 的真实理由，不自造。 */
function wkApprovals(){
  const box=$('#wkapbox'); if(!box) return;
  if(!WKREC.length){
    box.innerHTML='<div class="aw"><span class="ap">'
      +'当前无非绿项，无需授权任何动作（来自 /__act/recommend 真判定）。</span></div>';
    return;
  }
  box.innerHTML=WKREC.map(r=>{
    const on=WKPEND.has(r.action);
    return '<div class="aw" data-why="'+esc(r.evidence)+'"><span class="an">'+esc(r.action)+'</span>'+
      '<span class="st '+(on?'on':'off')+'">'+(on?'已授权':'待授权')+'</span>'+
      '<span class="ap">'+esc(r.why)+'</span>'+
      '<button class="abtn" data-wkap="'+esc(r.action)+'"'
      +' aria-label="'+(on?'撤销授权 ':'授权 ')+esc(r.action)+'">'+(on?'撤销':'授权')+'</button>'+
      '<button class="abtn" data-wk="'+esc(r.action)+'"'
      +(on&&!actBusy?'':' disabled')
      +' aria-label="执行已授权的 '+esc(r.action)+'">执行</button>'
      +'<span class="why">证据 '+esc(r.evidence)+'</span></div>';
  }).join('');
}
/* 指纹 diff：/__health 的 code_sha/index/data 三指纹逐次比对，
   只有「与上一轮不同」才标 chg —— 静止的指纹不算 diff，不许假装有变更。 */
const WKFPK=[['code_sha','serve.py 代码指纹'],['index','index.html 指纹'],['data','data.json 指纹']];
function wkDiff(prev,cur){
  const box=$('#wkdfbox'); if(!box) return;
  box.innerHTML=WKFPK.map(k=>{
    const a=(cur||{})[k[0]], b=(prev||{})[k[0]];
    const chg=!!(prev&&a&&b&&a!==b);
    const tail=chg?(' <span class="chg">变了（原 '+esc(b)+'）</span>'):'';
    return '<tr><th scope="row">'+esc(k[1])+'</th><td>'+esc(a||'不可判定')+
      '</td><td>'+(chg?tail:'未变')+'</td></tr>';
  }).join('');
  const ev=WKEV.slice(-8).reverse(), evb=$('#wkevbox');
  $('#wkevh').textContent='/__watch 事件（最近 '+ev.length+' 条）';
  evb.innerHTML=
    (ev.length?ev.map(e=>'<li><span class="dn">'+esc(e.t)+'</span>'+
      '<span class="dn2">'+esc(e.name)+'</span></li>').join('')
    :'<li><span class="dn2">本会话内尚无 __watch 事件（静止不等于有问题）</span></li>');
}
async function wkHealth(){
  try{
    const r=await fetch('/__health',{cache:'no-store'});
    const j=await r.json();
    const prev=WKFP; WKFP=j;
    $('#wkdfct').textContent='/__health 三指纹 · 常驻实例 clients '+j.clients+' · 上次动作 '
      +(j.act_last?(j.act_last.action+' rc='+j.act_last.rc):'无');
    if(j.act_last) WKSEEN.set(j.act_last.action,{rc:j.act_last.rc,dur:j.act_last.dur});
    wkDiff(prev,j); wkTools();
  }catch(e){ wkDiff(null,null); wkhint('/__health 不可达：'+e.message,'no'); }
}
/* 工具执行：走审批 → 走 /__act/stream 边跑边出字 → 写回 WKSEEN 真 rc/dur。
   未授权一律不发请求（授权卡就是这道闸，不做「先斩后奏」）。 */
async function wkRun(name){
  if(actBusy) return;
  if(WKREC.length&&!WKPEND.has(name)){
    wkhint(name+' 未授权 —— 先在审批卡点「授权」，再执行','no'); return;
  }
  actBusy=true; wkTools(); wkApprovals(); wkhint(name+' 运行中…');
  wklog('▶ '+name,'u');
  let es=null, seen=0;
  WKRUN=''; wkStopBtn(true);
  try{
    es=new EventSource('/__act/stream');
    es.onmessage=e=>{
      if(!e.data||e.data==='stream'||e.data.indexOf('__RC__ ')===0) return;
      if(e.data.indexOf('__RUN__ ')===0){   // 中止靠 run id 指名道姓，收到才给停止入口
        WKRUN=e.data.slice(8).trim(); wkStopBtn(true); return;
      }
      wklog(e.data); seen++;
    };
  }catch(err){ /* 无 SSE 则退回回执 tail，不假装有流 */ }
  let j;
  try{
    const r=await fetch('/__act',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({action:name})});
    j=await r.json();
  }catch(e){ j={ok:false,rc:-1,tail:['NETWORK '+e.message]}; }
  if(es) es.close();
  actBusy=false;
  WKRUN=''; wkStopBtn(false);
  (j.tail||[]).forEach(t=>{ if(seen<40) wklog(t); });
  WKSEEN.set(name,{rc:j.rc,dur:j.dur});
  WKRCPT++;                       // 回执落账计数：中止流靠它判断「回执到了没有」
  if(j.aborted){
    wklog('⏹ '+name+' 被用户中止（run='+(j.run||'未知')+'）—— 不记成功','e');
    wkhint(name+' 已中止 rc='+j.rc+'（不算成功）','no');
  }else{
    wklog('◀ '+name+' rc='+j.rc+' 用时 '+j.dur+'s（流式 '+seen+' 行）',j.rc===0?'':'e');
    wkhint(name+' rc='+j.rc+' · '+(j.ok?'成功':'失败'),j.ok?'ok':'no');
  }
  wkTools(); wkApprovals(); wkHealth();
}
/* 中止：只对**当前这个 run** 生效（run id 由 /__act/stream 的 __RUN__ 帧给）。
   run id 没到手就不给按钮 —— 不盲杀。服务端 abort_act 杀整组进程并把 rc 归一到 130，
   绝不把「用户不想等了」记成 rc=0 成功。 */
let WKRUN='', WKRCPT=0;
function wkStopBtn(on){
  const b=$('#wkstop'); if(!b) return;
  b.hidden=!(on&&WKRUN);
  b.disabled=!WKRUN;
  b.setAttribute('aria-label',WKRUN?('中止正在运行的动作 run='+WKRUN):'中止正在运行的动作');
}
async function wkAbort(){
  if(!WKRUN){ wkhint('还没有可中止的 run（run id 未到手）','no'); return; }
  const run=WKRUN, b=$('#wkstop'), rcpt0=WKRCPT;
  b.disabled=true; wkhint('正在中止 run='+run+'…');
  let j={ok:false};
  try{
    const r=await fetch('/__act/abort',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({run:run})});
    j=await r.json();
  }catch(e){ j={ok:false,error:'NETWORK '+e.message}; }
  /* 中止回执与动作回执是两条异步流，谁后到谁写 hint —— D76 实测踩过：
     livecheck 被真中止时，wkAbort 的「已发中止」晚 0.1s 落地，把 wkRun 已经写好的
     「已中止 rc=130（不算成功）」盖掉了，用户最后只看到「已发中止」，rc 不见了。
     裁决：回执（actBusy 已落 = wkRun 收尾）是权威，只在它还没到时才由本流补一句。*/
  if(!j.ok) wkhint('中止未生效：'+(j.error||'HTTP 未知'),'no');
  else if(WKRCPT===rcpt0) wkhint('已发中止 run='+run+'（rc 记 130，不算成功）','no');
  if(!WKRUN) b.disabled=false;
}
/* composer：斜杠命令 = allowlist 动作的直接映射（/rebuild → rebuild），
   与 ⌘K 命令面板共存分工 —— ⌘K 找视图/阶段，composer 发动作。 */
function wkParse(q){
  const s=(q||'').trim();
  if(!s) return null;
  if(s==='/help'||s==='?') return {help:true};
  if(s[0]==='/'){
    const n=s.slice(1).split(/\s+/)[0];
    return {act:ACTS.find(a=>a[0]===n)?n:null,bad:n};
  }
  const n=s.split(/\s+/)[0];
  return {act:ACTS.find(a=>a[0]===n)?n:null,bad:n};
}
async function wkSubmit(){
  const q=$('#wkq'), p=wkParse(q.value);
  if(!p){ wkhint(''); return; }
  if(p.help){
    q.value='';
    wkhint('可用动作 '+ACTS.map(a=>'/'+a[0]).join(' · '));
    wklog('/help → 列出 '+ACTS.length+' 个 allowlist 动作','sys');
    return;
  }
  const raw=q.value.trim();
  if(p.act){ q.value=''; await wkRun(p.act); return; }
  /* 自然语言兜底：交给服务端词典判一次（只读路由，不授权）。
     exact 判不出的句子才走这里；命中 → 如实告诉用户「这句话被理解成哪个动作」
     （透明，别把路由做成黑箱）；歧义 → 摆候选让他选；听不懂 → 诚实说听不懂。
     路由 ≠ 授权：wkRun 仍会被审批卡挡住。 */
  let j=null;
  try{
    const r=await fetch('/__act/resolve?q='+encodeURIComponent(raw),{cache:'no-store'});
    j=await r.json();
  }catch(e){ wkhint('/__act/resolve 不可达：'+e.message,'no'); return; }
  if(j&&j.ok&&j.action){
    q.value='';
    const via=(j.how==='intent')?'（按你说的「'+j.matched.join('+')+'」路由）':'';
    wklog('「'+raw+'」→ '+j.action+via,'sys');
    wkhint('「'+raw+'」理解为 '+j.action+' —— '+((j.how==='intent')
      ?'靠命中的关键词路由；仍需下方审批卡授权':'动作名直接命中'),'ok');
    await wkRun(j.action);
  }else if(j&&j.reason==='ambiguous'){
    const c=(j.candidates||[]).map(x=>x[0]+'('+x[1]+')').join(' · ');
    wkhint('「'+raw+'」有歧义，不替你猜：'+c+' —— 请用 /动作名 明确','no');
    wklog('「'+raw+'」歧义 → '+c,'sys');
  }else{
    const why=(j&&j.reason==='empty')?'是空的':'不在任何动作词典里';
    wkhint('「'+raw+'」'+why+' —— 听不懂，不乱猜；用 /动作名 或 /help','no');
    wklog('「'+raw+'」no-match（'+why+'）','sys');
  }
}
async function wkLoad(){
  wkTools();
  $('#wktrbox').innerHTML='<div class="idle">未运行。'
    +'执行只认服务端 allowlist，rc 为唯一裁决；点工具卡片或用下方 composer 发起。</div>';
  wkDiff(null,null);
  try{
    const r=await fetch('/__act/recommend',{cache:'no-store'});
    const j=await r.json();
    WKREC=Array.isArray(j.recommend)?j.recommend:[];
  }catch(e){ WKREC=[]; wkhint('/__act/recommend 不可达：'+e.message,'no'); }
  wkApprovals(); wkHealth();
  if(!WKPOLL) WKPOLL=setInterval(()=>{ if($('#v-work').classList.contains('on')) wkHealth(); },20000);
}

/* ── D77 第 7 视图 目标台 ───────────────────────────
   闭环第一环：一句自然语言 → 引擎真编译成结构化计划 → 独立任务空间落盘。
   为什么不复用 Agent 台那套动作表：ACTIONS 是固定 9 个白名单动作，
   「用户给目标→系统把目标变成现实」根本不在里面 —— 这一环在 D77 之前是空的。
   这里的「编译」也不是前端拿正则拼一张像计划的东西：POST /__goal/plan 后端真 fork
   公司引擎的 goal_loop.py set-goal，rc 与产物都由引擎说了算，前端只如实显示。
   隔离纪律：run id / hint / transcript / 计划态全部独立于 Agent 台（GRUN/GHI/…），
   两台并行跑时不许互相把对方的收尾盖掉（D76 踩过这个坑）。 */
const GHI=[];              // 目标台 hint 历史（独立于 WKHI，门要判顺序时用）
let GRUN='', GBUSY=false, GLAST=null;
function ghint(txt,kind){
  const el=$('#ghint'); if(!el) return;
  GHI.push({t:Date.now(),txt:String(txt||'')});
  if(GHI.length>60) GHI.shift();
  el.innerHTML=txt?('<span class="'+(kind||'')+'">'+esc(txt)+'</span>'):'';
}
function glog(txt,kind){
  const box=$('#gtrbox'); if(!box) return;
  const idle=box.querySelector('.idle'); if(idle) idle.remove();
  const d=document.createElement('div');
  d.className='m '+(kind||'');
  d.textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false})+'  '+txt;
  box.appendChild(d);
  while(box.children.length>200) box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}
/* textarea → 每行一条。空行直接丢（不是需求，用户只是敲了回车）；
   前导 '- ' 剥掉（Markdown 列表习惯）。裸 '-' 开头的其它形式不剥 ——
   服务端 _goal_validate 会挡掉并如实报错，前端不偷偷改写用户输入。*/
function glines(id){
  const el=$(id); if(!el) return [];
  return el.value.split(/\r?\n/).map(s=>s.trim().replace(/^[-*]\s+/,''))
    .filter(s=>s.length);
}
function gstopBtn(on){
  const b=$('#gdstop'); if(!b) return;
  b.hidden=!(on&&GRUN);
  b.disabled=!GRUN;
  b.setAttribute('aria-label',GRUN?('中止正在编译的目标计划 run='+GRUN):'中止正在编译的目标计划');
}
/* 只画引擎给的东西。rc 不为 0 时 plan 是 null —— 那一格必须写「引擎判红」，
   绝不渲染一张空 AC 卡片（那看起来像「这个目标没有 AC」，与事实相反）。 */
function gplanBox(j){
  const box=$('#gplan'); if(!box) return;
  const pass=(j.verdict==='PASS');
  const tag=pass?'<span class="gst pass">PASS rc=0</span>'
    :`<span class="gst block">BLOCK rc=${esc(j.rc)}</span>`;
  let h=`<div class="gcard ${pass?'gpass':'gblock'}"><h4>编译结果${tag}`
    +`<span class="gst">${esc(j.taskdir||'')}</span></h4>`;
  if(!pass){
    h+=`<p class="ggoal">引擎目标门判红：${esc(j.error||'rc 不为 0')}</p>`;
    const t=(j.tail||[]).map(x=>`<li><span class="gn">·</span>${esc(x)}</li>`).join('');
    if(t) h+=`<ul class="glist">${t}</ul>`;
    h+='<div class="gmeta"><span>引擎原话逐行在下面「编译现场」里可查；'
      +'台面不替它改写成「差一点」。</span></div></div>';
    box.innerHTML=h; return;
  }
  const p=j.plan||{};
  h+=`<p class="ggoal">${esc(p.goal||p.demand||'')}</p>`;
  const acs=(p.acs||[]).map((a,i)=>
    `<li><span class="gn">AC${i+1}</span><span>${esc(a)}</span></li>`).join('');
  const empty='<li><span class="gn">—</span><span>引擎没给出 AC'
    +'（不该发生：rc=0 但产物缺 AC 即假绿）</span></li>';
  h+=`<h4>验收标准<span class="gst">${(p.acs||[]).length} 条</span></h4>`
    +`<ul class="glist" id="gaclist">${acs||empty}</ul>`;
  const gt=p.goal_table||[];
  if(gt.length){
    const rows=gt.map(r=>
      `<li><span class="gn">R${esc(r.n)}</span><span>${esc(r.goal||'')}</span></li>`).join('');
    h+=`<h4>轮次计划<span class="gst">${gt.length} 轮</span></h4>`
      +`<ul class="glist" id="grlist">${rows}</ul>`;
  }
  const ng=(p.non_goals||[]).map(x=>
    `<li><span class="gn">非</span><span>${esc(x)}</span></li>`).join('');
  if(ng) h+='<h4>非目标<span class="gst">防范围蔓延</span></h4>'
    +`<ul class="glist" id="gnlist">${ng}</ul>`;
  h+='<div class="gmeta">'
    +`<span><b>引擎</b> ${esc(p.version||'—')}</span>`
    +`<span><b>状态</b> ${esc(p.state||'—')}</span>`
    +`<span><b>部门</b> ${esc((p.depts||[]).join(' · ')||'—')}</span>`
    +`<span class="gpath" id="gpath"><b>任务空间</b> ${esc(j.taskdir||'')}</span></div>`;
  h+='<div class="gmeta"><span>产物 '
    +`${esc((j.artifacts||[]).join(' · ')||'—')}`
    +'（草稿，未绑活项目目标链）</span></div></div>';
  box.innerHTML=h;
}
function goalLoad(){
  $('#gtrbox').innerHTML='<div class="idle">未编译。填需求 + AC + 非目标后点「编译计划」；'
    +'判红就是判红，台面不替你猜需求够不够具体。</div>';
  gstopBtn(false); GRUN='';
  ghint(''); GLAST=null;
}
async function goalSubmit(){
  if(GBUSY){ ghint('已有一次编译在跑（单飞）—— 等它跑完或先中止','no'); return; }
  const demand=($('#gdmd').value||'').trim();
  const acs=glines('#gdac'), ngs=glines('#gdng');
  let rounds=parseInt($('#gdrn').value,10);
  if(!isFinite(rounds)) rounds=3;
  /* 前端只挡「空需求」——形状以外的判定权归引擎。
     在这里复刻 _quality 的语义规则（单 AC / 无非目标 / 占位词…）就是造第二套真相：
     引擎升版改了判据，台面会静默按旧规则说另一套话。 */
  if(!demand){ ghint('需求是空的 —— 台面不替你编一个','no'); return; }
  GBUSY=true; $('#gdgo').disabled=true; gstopBtn(false); GRUN='';
  ghint('编译中…（真 fork 公司引擎 set-goal）');
  glog('▶ 编译计划：'+demand,'u');
  $('#gplan').innerHTML='';
  let es=null, seen=0;
  try{
    es=new EventSource('/__act/stream');
    es.onmessage=e=>{
      if(!e.data||e.data==='stream'||e.data.indexOf('__RC__ ')===0) return;
      if(e.data.indexOf('__RUN__ ')===0){ GRUN=e.data.slice(8).trim(); gstopBtn(true); return; }
      glog(e.data); seen++;
    };
  }catch(err){ /* 无 SSE 就退回回执 tail，不假装有流 */ }
  let j;
  try{
    const r=await fetch('/__goal/plan',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({demand:demand,acs:acs,non_goals:ngs,rounds:rounds})});
    j=await r.json();
  }catch(e){ j={ok:false,rc:-1,verdict:'BLOCK',error:'NETWORK '+e.message,tail:[]}; }
  if(es) es.close();
  GBUSY=false; $('#gdgo').disabled=false;
  GRUN=''; gstopBtn(false);
  (j.tail||[]).forEach(t=>{ if(seen<40) glog(t); });
  GLAST=j; gplanBox(j);
  if(j.verdict==='PASS'){
    /* 编译出的就是刚建的任务空间：把轮次链的默认源指过去。
       否则用户编译完还得手动抄一遍路径才能读台账 —— 而这条链（D78 补的第二环）
       正是为了让「编译 → 读逐轮台账 → 下一轮该做什么」在一屏里连起来。 */
    if(j.taskdir) $('#gdir').value=j.taskdir;
    glog(`◀ rc=0 用时 ${j.dur}s（流式 ${seen} 行）→ 计划已渲染`);
    ghint(`引擎编译通过 rc=0 · ${((j.plan||{}).acs||[]).length} 条 AC`
      +` · 草稿落在 ${j.taskdir||'?'}`,'ok');
  }else{
    glog(`◀ rc=${j.rc} → 引擎判红，不记成功`,'e');
    ghint(`BLOCK rc=${j.rc}：${j.error||'引擎判红'}`,'no');
  }
}
async function goalAbort(){
  if(!GRUN){ ghint('还没有可中止的 run（run id 未到手）','no'); return; }
  const run=GRUN, b=$('#gdstop');
  b.disabled=true; ghint(`正在中止 run=${run}…`);
  let j={ok:false};
  try{
    const r=await fetch('/__act/abort',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({run:run})});
    j=await r.json();
  }catch(e){ j={ok:false,error:'NETWORK '+e.message}; }
  if(!j.ok) ghint('中止未生效：'+(j.error||'HTTP 未知'),'no');
  else ghint(`已发中止 run=${run}（rc 记 130，不算成功）`,'no');
  if(!GRUN) b.disabled=false;
}

/* ---- D78 轮次链：闭环第二环 ----
   纪律与编译面一致：三个入口都是**只读**的（status / next-round / round-goal），
   台面不提供收轮/绑目标这类写操作 —— 那些要对话 goal id 和本轮专属账，
   在浏览器里替用户编一个就把闭环演成假的。
   引擎说 rc=0 但读不出轮次表时，报「读不到」而不是画一张空表。 */
let CHBUSY=false, CHLAST=null;
function chhint(txt,kind){
  const el=$('#gchint'); if(!el) return;
  el.innerHTML=txt?('<span class="'+(kind||'')+'">'+esc(txt)+'</span>'):'';
}
function chlog(txt,kind){
  const box=$('#gtrbox'); if(!box) return;
  const idle=box.querySelector('.idle'); if(idle) idle.remove();
  const d=document.createElement('div');
  d.className='m '+(kind||'');
  d.textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false})+'  '+txt;
  box.appendChild(d);
  while(box.children.length>200) box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}
function chfail(j){
  const box=$('#gchain'); if(!box) return;
  const msg=j.ledger_error||j.error||('rc='+(j.rc===undefined?'?':j.rc));
  box.innerHTML='<div class="gcard gblock"><h4>读不到轮次链'
    +'<span class="gst block">BLOCK</span></h4>'
    +`<p class="ggoal">${esc(msg)}</p>`
    +'<div class="gmeta"><span>引擎原话逐行在下面「编译现场」里可查；'
    +'台面不替它补一张空表。</span></div></div>';
  (j.tail||[]).forEach(t=>chlog('· '+t,'e'));
  chhint('BLOCK：'+msg,'no');
}
function chtable(led){
  const rows=(led.table||[]).map(r=>
    `<tr data-st="${esc(r.state||'')}">`
    +`<td class="gn-cell">R${esc(r.n)}</td>`
    +`<td class="gst-cell">${esc(r.state||'—')}</td>`
    +`<td class="gn-cell">${esc(r.giga_run||'—')}</td>`
    +`<td class="gwrap">${esc(r.goal||'—')}</td>`
    +`<td class="gwrap">${esc(r.ac||'—')}</td>`
    +`<td class="gwrap">${esc(r.delta||'（未收轮）')}</td></tr>`).join('');
  return '<table class="gtbl"><caption class="sr">目标表：每轮一行，'
    +'提升列由引擎 close-round 写入</caption><thead><tr>'
    +'<th scope="col">轮次</th><th scope="col">状态</th><th scope="col">本轮账</th>'
    +'<th scope="col">目标</th><th scope="col">判据</th><th scope="col">提升</th>'
    +'</tr></thead><tbody>'+(rows||'<tr><td colspan="6">引擎没给轮次表</td></tr>')
    +'</tbody></table>';
}
function cdraw(j){
  const box=$('#gchain'); if(!box) return;
  if(!j.ok){ chfail(j); return; }
  const p=j.payload||{};
  if(j.op==='status'){
    const led=j.ledger||{};
    const ch=(led.chain||[]).map(c=>
      `<li><span class="gn">R${esc(c.n)}</span><span>${esc(c.goal_id||'未绑')}</span></li>`).join('');
    const rs=(led.rounds||[]).map(r=>
      `<li><span class="gn">R${esc(r.n)}</span><span>${esc(r.state||'—')}`
      +` · verdict ${esc(r.verdict||'—')} · 账 ${esc(r.giga_run||'—')}</span></li>`).join('');
    box.innerHTML='<div class="gcard gpass"><h4>轮次台账'
      +`<span class="gst pass">rc=0</span><span class="gst">${esc(j.taskdir||'')}</span></h4>`
      +`<p class="ggoal">${esc(led.demand||'')}</p>`
      +chtable(led)
      +'<h4>开过的轮次<span class="gst">'+(led.rounds||[]).length+' 轮</span></h4>'
      +`<ul class="glist" id="gcrounds">${rs||'<li><span class="gn">—</span>'
        +'<span>还没开过轮（正常：先 bind-goal 立对话目标）</span></li>'}</ul>`
      +'<h4>逐轮对话目标链<span class="gst">'+(led.chain||[]).length+' 轮已立</span></h4>'
      +`<ul class="glist" id="gcchain">${ch||'<li><span class="gn">—</span>'
        +'<span>未绑定对话目标 —— 目标只写在文档里不算数</span></li>'}</ul>`
      +'<div class="gmeta">'
      +`<span><b>状态</b> ${esc(led.state||'—')}</span>`
      +`<span><b>轮次计划</b> ${esc(led.rounds_plan===undefined?'—':led.rounds_plan)}</span>`
      +`<span><b>AC</b> ${(led.acs||[]).length} 条</span>`
      +`<span><b>非目标</b> ${(led.non_goals||[]).length} 条</span>`
      +`<span><b>耗时</b> ${esc(j.dur)}s</span></div></div>`;
    chhint(`台账已渲染 · ${(led.table||[]).length} 轮 · 状态 ${led.state||'—'}`,'ok');
    return;
  }
  /* next-round / round-goal：只画引擎的原话与它给的 objective。
     台面自己写一句「下一步该……」就是在替引擎发言，所以逐字转述。 */
  const cg=p.create_goal_arg;
  box.innerHTML='<div class="gcard gpass"><h4>'
    +(j.op==='next-round'?'下一轮该做什么':'本轮目标原文')
    +`<span class="gst pass">rc=0</span><span class="gst">${esc(j.taskdir||'')}`
    +`${j.op==='round-goal'?' · R'+esc(j.round):''}</span></h4>`
    +`<p class="ggoal">${esc(p.msg||'')}</p>`
    +(cg?`<h4>交给对话的目标原文<span class="gst">create_goal 逐字用</span></h4>`
      +`<pre class="gcode" id="gcobj">${esc(cg)}</pre>`:'')
    +(p.next?`<h4>引擎指的下一步</h4><pre class="gcode" id="gcnext">${esc(p.next)}</pre>`:'')
    +`<div class="gmeta"><span>耗时 ${esc(j.dur)}s</span>`
    +`<span>${(p.table_total===undefined?'':
        `目标表 ${esc(p.table_done)}/${esc(p.table_total)} 已收`)}</span>`
    +(p.next_round?`<span>下一轮 R${esc(p.next_round)}</span>`:'')
    +(p.zero_delta_warn?`<span>零增量告警 ${esc(p.zero_delta_warn)}</span>`:'')
    +'</div></div>';
  (j.tail||[]).forEach(t=>{ if(t.trim()) chlog('· '+t); });
  chhint(j.op==='next-round'?'引擎已给出下一轮该做什么（rc=0）':'引擎已给出本轮目标原文（rc=0）','ok');
}
async function chainOp(op){
  if(CHBUSY){ chhint('上一次轮次链查询还在跑 —— 等它回来','no'); return; }
  const dir=($('#gdir').value||'').trim();
  let n=parseInt($('#grn').value,10); if(!isFinite(n)) n=1;
  CHBUSY=true;
  $('#gcst').disabled=true; $('#gcnx').disabled=true; $('#gcrg').disabled=true;
  chhint('读轮次链…（真 fork 公司引擎只读入口）');
  chlog('▶ '+op+(dir?(' @ '+dir):' @ 活项目根'),'u');
  $('#gchain').innerHTML='';
  let j;
  try{
    const r=await fetch('/__goal/chain',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({op:op,taskdir:dir,round:n})});
    j=await r.json();
  }catch(e){ j={ok:false,rc:-1,verdict:'BLOCK',error:'NETWORK '+e.message,tail:[]}; }
  CHBUSY=false;
  $('#gcst').disabled=false; $('#gcnx').disabled=false; $('#gcrg').disabled=false;
  if(j.error&&!j.tail) j.tail=[];
  chlog('◀ '+op+' rc='+(j.rc===undefined?'-':j.rc)+' 用时 '+(j.dur===undefined?'?':j.dur)+'s','u');
  CHLAST=j; cdraw(j);
}
function chainLoad(){
  $('#gchain').innerHTML=''; chhint(''); CHLAST=null;
}
/* ---- D79 收轮就绪度预检：闭环第三环 ----
   与第二环相反：这一环的面是**坏消息**。收轮是唯一一步会写 GOAL.json /
   轮账 / 目标表的操作，引擎九条硬门（delta 非占位、evidence 带 rc、禁口头
   断言、指名 AC、SHIP 不许有未收轮、ROUND-BIND sha 复算、G0 真产物…）任一条
   红就 rc=2，用户往往是一路写完才发现。所以台面只做**只读预判**：把你打算写的
   四个字贴进来，逐条告诉你引擎会怎么判、缺哪一项。
   它绝不代收轮 —— 预检通过只给一行 close-round 命令，要不要真跑由人决定。 */
let PFBUSY=false, PLLAST=null;
function phint(txt,kind){
  const el=$('#gphint'); if(!el) return;
  el.innerHTML=txt?('<span class="'+(kind||'')+'">'+esc(txt)+'</span>'):'';
}
function preflightReset(){
  /* 只清**渲染**不清输入框：判据绑的是 (任务空间, 轮次, 草案)，三者任一变了
     屏上那份结论就过期；但用户打的字是他的活儿，切个 tab 就抹掉是台面在管闲事。 */
  const box=$('#gpre'); if(box) box.innerHTML='';
  PLLAST=null; phint('');
}
function pglist(gs){
  return '<ul class="glist">'+gs.map(g=>
    `<li data-ok="${g.ok?1:0}"><span class="gn">${esc(g.id)}</span>`
    +`<span class="gst ${g.ok?'pass':'block'}">${g.ok?'PASS':'BLOCK'}</span>`
    +`<span class="gw"><b>${esc(g.label)}</b>`
    +`<span class="gwhy">${esc(g.why)}</span></li>`).join('')+'</ul>';
}
function actGrid(){
  $('#actgrid').innerHTML=ACTS.map(a=>
    '<div class="a1"><div class="n">'+esc(a[0])+'</div><div class="d">'+esc(a[1])+'</div>'+
    '<button class="abtn" data-act="'+esc(a[0])+'" aria-label="运行 '+esc(a[0])+'：'+esc(a[1])+'">运行</button></div>'
  ).join('');
  if(!$('#ares').dataset.init){
    $('#ares').dataset.init='1';
    $('#ares').innerHTML='<div class="idle">未运行。执行只认服务端 allowlist，rc 为唯一裁决。</div>';
  }
}

async function actLoad(){
  try{
    const r=await fetch('/__act',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({action:'__list'})});
    const j=await r.json();
    if(Array.isArray(j.actions)&&j.actions.length){
      ACTS=j.actions.map(a=>[a.name,a.desc]); actGrid();
    }
  }catch(e){ /* 兜底表已在，静态直开也能看见台面 */ }
}

/* 离线快照兜底（D65，2026-10-03 实测缺陷）：
   交付物就是一份 index.html，直开 file:// 时 fetch('/__promote') 必然 Failed to fetch，
   于是「决策 / 晋升 / 蒸馏」三环——也就是本驾驶舱最核心的自我进化闭环——在交付态全死，
   而 view_dod 仍判它「有真内容」（那三处的降级文案本身就是非空文本）→ 判据漏了。
   构建期快照（tools/snap_live.py）在这里兜底，三条硬规矩：
     1. 快照**必须自报非实时**并带捕获时刻——快照冒充实时就是撒谎；
     2. 快照里 ok=false 照原样渲染（红就是红），不因为「离线兜底」而洗绿；
     3. 快照缺失就明说「无快照」，不静默留空、不编数。
   占位块刻意**不用 .e1 / .rc1**（那是数据行的类）：否则 snap_gate 的行计数器
   会把「无快照」当成真数据行，「不编行」这条判据就被自己糊弄过去了。 */
const SNAP=(D&&D.live_snapshot)||null;
function snapAgeS(){
  if(!SNAP||!SNAP.captured_at) return null;
  const t=Date.parse(SNAP.captured_at);
  if(isNaN(t)) return null;
  return Math.max(0,Math.round((Date.now()-t)/1000));
}
function snapTag(){
  const a=snapAgeS();
  if(a==null) return '';
  const over=a>(Number(SNAP.stale_after_s)||3600);
  return ' · 快照 '+esc(String(SNAP.captured_at).slice(5,19).replace('T',' '))+
    ' · '+(over?'已过期':'非实时');
}
function noSnap(box,kind,ct){
  if(ct) $(ct).textContent='无快照：需服务在线，或先跑 tools/snap_live.py 再重建台面';
  box.innerHTML='<div class="nosnap">'+esc(kind)+' 无快照（需服务在线）</div>';
  return false;
}

async function promoLoad(){
  const box=$('#promo');
  box.innerHTML='<div class="e1"><div class="k">reading</div><div class="v">正在读晋升台账…</div></div>';
  try{
    const r=await fetch('/__promote');
    const j=await r.json();
    const list=Array.isArray(j.rows)?j.rows:[];
    $('#promoct').textContent='闭环的「晋升」一环 · 共 '+j.total+' 条 · 就绪 '+(j.n_ready==null?'?':j.n_ready)+
      ' · 拦截 '+(j.n_hold==null?'?':j.n_hold)+' · 快照 '+(j.age_s==null?'?':j.age_s)+'s 前'+(j.cached?'（缓存）':'');
    box.innerHTML=list.length?list.map(x=>
      '<div class="e1"><div class="k">'+esc(x.status)+' · '+(x.ready?'READY':'HOLD')+
      '</div><div class="v">'+esc(x.id)+'</div>'+
      '<div class="v">'+esc(x.why||'')+'</div></div>').join('')
      :'<div class="e1"><div class="k">promote</div><div class="v">空</div></div>';
  }catch(e){
    const j=SNAP&&SNAP.promote;
    if(!j) return noSnap(box,'promote','#promoct');
    const rows=Array.isArray(j.rows)?j.rows:[];
    $('#promoct').textContent='闭环的「晋升」一环 · 共 '+(j.total==null?'?':j.total)+
      ' 条 · 就绪 '+(j.n_ready==null?'?':j.n_ready)+' · 拦截 '+(j.n_hold==null?'?':j.n_hold)+
      (j.ok?'':' · 引擎判红：'+esc(j.error||'ok=false'))+snapTag();
    box.innerHTML=rows.length?rows.map(x=>
      '<div class="e1"><div class="k">'+esc(x.status)+' · '+(x.ready?'READY':'HOLD')+
      '</div><div class="v">'+esc(x.id)+'</div>'+
      '<div class="v">'+esc(x.why||'')+'</div></div>').join('')
      :'<div class="e1"><div class="k">promote</div><div class="v">快照内无行'+
       (j.error?'（'+esc(j.error)+'）':'')+'</div></div>';
  }
}

async function distillLoad(){
  const box=$('#dist');
  box.innerHTML='<div class="e1"><div class="k">reading</div><div class="v">正在读蒸馏账本…</div></div>';
  try{
    const r=await fetch('/__distill');
    const j=await r.json();
    const list=Array.isArray(j.rows)?j.rows:[];
    $('#distct').textContent='蒸馏一环 · 共 '+j.n_rows+' 行 · 双证据 '+
      (j.n_both==null?'?':j.n_both)+'/'+(j.target==null?'?':j.target)+
      (j.verify_ok?' · 闸绿':' · 闸红');
    box.innerHTML=list.length?list.map(x=>
      '<div class="e1"><div class="k">'+esc(x.repo)+' · '+esc(x.state)+
      '</div><div class="v">star '+esc(x.stars)+' · '+esc(x.license)+' · '+esc(x.commit)+
      '</div></div>').join('')
      :'<div class="e1"><div class="k">distill</div><div class="v">空</div></div>';
  }catch(e){
    const j=SNAP&&SNAP.distill;
    if(!j) return noSnap(box,'distill','#distct');
    const rows=Array.isArray(j.rows)?j.rows:[];
    $('#distct').textContent='蒸馏一环 · 共 '+(j.n_rows==null?'?':j.n_rows)+' 行 · 双证据 '+
      (j.n_both==null?'?':j.n_both)+'/'+(j.target==null?'?':j.target)+
      (j.verify_ok?' · 闸绿':' · 闸红')+
      (j.ok?'':' · 引擎判红：'+esc(j.error||'ok=false'))+snapTag();
    box.innerHTML=rows.length?rows.map(x=>
      '<div class="e1"><div class="k">'+esc(x.repo)+' · '+esc(x.state)+
      '</div><div class="v">star '+esc(x.stars)+' · '+esc(x.license)+' · '+esc(x.commit)+
      '</div></div>').join('')
      :'<div class="e1"><div class="k">distill</div><div class="v">快照内无行'+
       (j.error?'（'+esc(j.error)+'）':'')+'</div></div>';
  }
}

async function recLoad(){
  const box=$('#recs');
  box.innerHTML='<span class="none">正在推导建议动作…</span>';
  try{
    const r=await fetch('/__act/recommend');
    const j=await r.json();
    const list=Array.isArray(j.recommend)?j.recommend:[];
    $('#recct').textContent='闭环的「决策」一环 · 总闸 '+esc(j.verdict)+'，推导 '+list.length+' 条';
    box.innerHTML=list.length?list.map(x=>
      '<div class="rc1 s-'+esc(x.severity)+'"><span class="sv">'+esc(x.severity)+'</span>'+
      '<span class="wy">'+esc(x.why)+'</span>'+
      '<button data-rec="'+esc(x.action)+'" aria-label="按建议运行 '+esc(x.action)+'：'+esc(x.why)+'">运行 '+esc(x.action)+'</button></div>'
    ).join(''):'<span class="none">无非绿项，无需动作。</span>';
  }catch(e){
    const j=SNAP&&SNAP.recommend;
    if(!j) return noSnap(box,'recommend','#recct');
    const list=Array.isArray(j.recommend)?j.recommend:[];
    $('#recct').textContent='闭环的「决策」一环 · 总闸 '+esc(j.verdict==null?'?':j.verdict)+
      '，推导 '+list.length+' 条'+snapTag();
    box.innerHTML=list.length?list.map(x=>
      '<div class="rc1 s-'+esc(x.severity)+'"><span class="sv">'+esc(x.severity)+'</span>'+
      '<span class="wy">'+esc(x.why)+'</span>'+
      '<button data-rec="'+esc(x.action)+'" aria-label="按建议运行 '+esc(x.action)+'：'+esc(x.why)+'">运行 '+esc(x.action)+'</button></div>'
    ).join(''):'<span class="none">无非绿项，无需动作（来自快照）。</span>';
  }
}

async function actRun(name){
  if(actBusy) return;
  actBusy=true;
  const bs=[...$('#actgrid').querySelectorAll('button')];
  bs.forEach(b=>b.disabled=true);
  const seen=[];
  $('#ares').innerHTML='<div class="hd"><span>'+esc(name)+'</span><span class="busy">运行中…</span></div><pre id="actpre"></pre>';
  const pre=$('#actpre');
  let es=null;
  try{                       // 长动作边跑边出字，不再是干等一个「运行中…」
    es=new EventSource('/__act/stream');
    es.onmessage=e=>{
      if(!e.data||e.data==='stream') return;
      if(e.data.indexOf('__RC__ ')===0) return;
      seen.push(e.data); pre.textContent=seen.join('\n');
      pre.scrollTop=pre.scrollHeight;
    };
  }catch(err){ /* 无 SSE 能力则退回回执里的 tail */ }
  let j;
  try{
    const r=await fetch('/__act',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({action:name})});
    j=await r.json();
  }catch(e){ j={ok:false,rc:-1,tail:['NETWORK '+e.message]}; }
  if(es) es.close();
  actBusy=false; bs.forEach(b=>b.disabled=false);
  const ok=j.ok===true;
  const body=seen.join('\n')||(j.tail&&j.tail.length?j.tail.join('\n'):(j.error||''));
  $('#ares').innerHTML='<div class="hd"><span>'+esc(name)+'</span>'+
    '<span class="rc '+(ok?'ok':'no')+'">rc='+esc(j.rc)+'</span>'+
    '<span>'+esc(j.dur)+'s</span></div><pre>'+esc(body)+'</pre>';
  if(ok&&(name==='rebuild'||name==='verify')) setTimeout(()=>location.reload(),900);
}

function strip(){
  const a=D.aggregate||{};
  const gv=D.global_verdict;
  const ok=gv==='PASS';
  $('#strip').innerHTML=
    '<span>总闸 <b class="'+(ok?'gateok':'gatebad')+'">'+esc(gv)+'</b></span>'+
    '<span class="hide-s">实测 '+(D.gate_counts&&D.gate_counts.PASS||0)+'P / '+
      (D.gate_counts&&D.gate_counts.PARTIAL||0)+'L / '+
      (D.gate_counts&&D.gate_counts.FAIL||0)+'F</span>'+
    '<span class="hide-s">自报 '+((D.declared_counts||{}).PASS||0)+'P / '+
      ((D.declared_counts||{}).PARTIAL||0)+'L / '+((D.declared_counts||{}).FAIL||0)+'F</span>'+
    '<span>分歧 <b class="'+(D.n_divergence?'gatebad':'gateok')+'">'+(D.n_divergence||0)+'</b></span>'+
    '<span class="sp"></span>'+
    '<span>证据 <b id="ftfresh" class="gateok">—</b></span>'+
    '<span>覆盖率 '+(D.coverage_complete?'57/57 齐':'不齐')+'</span>'+
    '<span>manifest '+(D.manifest?D.manifest.present+'/'+D.manifest.n_total:'?')+'</span>'+
    '<span>降级 '+(D.env_gated||[]).length+'</span>';
  freshnessFoot();
}

/* ── 命令面板 ───────────────────────────── */
const VIEWS=[['over','总览'],['round','轮次台账'],['mx','阶段矩阵'],['led','证据账本'],
  ['eng','引擎与规则'],['work','Agent 台'],['goal','目标台'],['retro','复盘台'],
  ['cap','能力地图'],['ws','工作区'],
  ['term','终端'],['file','文件'],['set','设置']];   // R7 新视图三面也进 ⌘K 名单
// D95：这份名单此前同样只列 8 项，漏掉 D88 加的「工作区」。漏掉的真实后果是
// ⌘K 里搜「工作区」「文件」「终端」「浏览器」全部零结果 —— 而这是产品里唯一的
// 键盘入口。用户以为功能不存在，其实功能在屏上只是搜不到。
let cf=-1, citems=[];
let cmdOpener=null;
function cmdOpen(){
  cmdOpener=document.activeElement;
  $('#cmd').classList.add('on'); $('#cmdq').value=''; cmdRun();
  $('#cmdq').focus();                                  // 焦点必须进输入框，否则键盘用户打不了字
}
function cmdClose(){
  $('#cmd').classList.remove('on');
  if(cmdOpener&&cmdOpener.isConnected) cmdOpener.focus();   // 焦点归还，不把人丢回 body
  cmdOpener=null;
}
function cmdRun(){
  const q=$('#cmdq').value.trim().toLowerCase();
  citems=[];
  VIEWS.forEach(v=>{ if(!q||v[1].toLowerCase().includes(q)) citems.push({t:v[1],s:'视图',f:()=>view(v[0])}); });
  (D.dirs||[]).forEach((d,i)=>{ if(!q||d.dir.toLowerCase().includes(q))
    citems.push({t:d.dir,s:'阶段 · 实测 '+d.gate+' / 自报 '+d.declared,f:()=>{view('mx');readout(i);}}); });
  if(!citems.length){ $('#cmdlist').innerHTML='<div class="cmdempty">无匹配</div>'; cf=-1; return; }
  $('#cmdlist').innerHTML=citems.slice(0,40).map((c,i)=>
    '<button data-i="'+i+'" id="cmdo-'+i+'" role="option" aria-selected="false" tabindex="-1"><span class="k">'+esc(c.t)+'</span>'+
    '<span class="h">'+esc(c.s)+'</span></button>').join('');
  $('#cmdlist').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    const c=citems[+b.dataset.i]; if(c){ c.f(); cmdClose(); }}));
  cf=0; cmdSync();                                     // 打开就有高亮，不用先按一次方向键
}
function cmdMove(k){
  const bs=[...$('#cmdlist').querySelectorAll('button')]; if(!bs.length) return;
  cf=Math.max(0,Math.min(bs.length-1,cf+k));
  cmdSync(); bs[cf].scrollIntoView({block:'nearest'});
}
// 焦点恒在输入框，靠 aria-activedescendant 播报当前项；Tab 因此不会逃出模态。
function cmdSync(){
  const bs=[...$('#cmdlist').querySelectorAll('button')];
  bs.forEach((b,i)=>{ const on=i===cf; b.classList.toggle('sel',on); b.setAttribute('aria-selected',on?'true':'false'); });
  const q=$('#cmdq');
  if(bs[cf]) q.setAttribute('aria-activedescendant',bs[cf].id); else q.removeAttribute('aria-activedescendant');
}
function cmdGo(){ const bs=[...$('#cmdlist').querySelectorAll('button')]; if(bs[cf]) bs[cf].click(); }

/* ── 启动 ───────────────────────────────── */
function boot(){
  if(D.__err){ showState('error',{detail:D.__err}); return; }
  if(!Array.isArray(D.dirs)||!D.dirs.length){ showState('empty'); return; }
  showState('loading');
  setTimeout(()=>{
    $('#states').classList.remove('on');
    freshness();
    $('#stamp').textContent=fmtT(D.generated_at)+' · '+D.dirs.length+' 阶段';
    verdictTag();
    rail(); divergence(); stats(); ovgate(); rounds(); matrix(); ledger(); engine(); strip();
    projInit();   // 项目空间：先读真清单，首屏不许摆占位项目
    cxGate(); cxHealth();
    /* 右栏轮询：单例 setInterval，句柄存在 CXT 上。写单例而不是「每处各起一个
       timer」是因为第二次进视图再插一个会让刷新频率翻倍，而执行状态的意义
       恰恰在于「新不新」，频率失控之后这一栏就没有可信度了。
       5000ms 与 /__health 的开销匹配（纯内存读，不碰磁盘树）。 */
    if(!CXT) CXT=setInterval(cxHealth,5000);
    wsInit();
    actGrid(); actLoad();
    wkLoad();
    recLoad();
    promoLoad();
    distillLoad();
    $('#actgrid').addEventListener('click',e=>{
      const b=e.target.closest('button[data-act]');
      if(b) actRun(b.dataset.act);
    });
    $('#recs').addEventListener('click',e=>{
      const b=e.target.closest('button[data-rec]');
      if(b) actRun(b.dataset.rec);
    });
    $('#wktools').addEventListener('click',e=>{
      const b=e.target.closest('button[data-wk]');
      if(b) wkRun(b.dataset.wk);
    });
    $('#wkapbox').addEventListener('click',e=>{
      const b=e.target.closest('button[data-wkap]'); if(!b) return;
      const n=b.dataset.wkap;
      if(WKPEND.has(n)) WKPEND.delete(n); else WKPEND.add(n);
      wkApprovals();
      wkhint(n+(WKPEND.has(n)?' 已授权，可执行':' 已撤销授权'));
    });
    $('#wksend').addEventListener('click',wkSubmit);
    $('#wkstop').addEventListener('click',wkAbort);
    $('#wkq').addEventListener('keydown',e=>{
      if(e.key==='Enter'){ e.preventDefault(); wkSubmit(); }
    });
    $('#gdgo').addEventListener('click',goalSubmit);
    $('#gdstop').addEventListener('click',goalAbort);
    $('#gcst').addEventListener('click',()=>chainOp('status'));
    $('#gcnx').addEventListener('click',()=>chainOp('next-round'));
    $('#gcrg').addEventListener('click',()=>chainOp('round-goal'));
    $('#gcpf').addEventListener('click',preflight);
    $('#rtgo').addEventListener('click',retroLoad);
    $('#capgo').addEventListener('click',capLoad);
    /* 搜索与筛选只重绘展示层，不重新读盘：判定权全在引擎账本里，
       前端没有任何一条路径能把一条 no-verdict 的账画成「已验过」。 */
    $('#capq').addEventListener('input',capRedraw);
    $('#capf').addEventListener('change',capRedraw);
    /* 判据绑死 (任务空间, 轮次)。任一变了，屏上那份就绪度就是**过期**的结论，
       而一份过期的「就绪」比没有更坏 —— 用户会照着一条属于上一轮的命令去收轮。
       所以这里清渲染（清掉那份过期结论），但不清他打的字。 */
    $('#gpn').addEventListener('input',()=>{ $('#grn').value=$('#gpn').value; preflightReset(); });
    $('#grn').addEventListener('input',()=>{ $('#gpn').value=$('#grn').value; preflightReset(); });
    $('#gdir').addEventListener('input',preflightReset);
    ['#gpn','#gpa'].forEach(sel=>{
      $(sel).addEventListener('keydown',e=>{
        if(e.key==='Enter'){ e.preventDefault(); preflight(); }
      });
    });
    /* 两个 textarea（delta/evidence）不吃裸 Enter：那里换行就是「再写一条判据」，
       沿用本视图已有约定 —— ⌘/Ctrl+Enter 提交。 */
    ['#gpdl','#gpev'].forEach(sel=>{
      $(sel).addEventListener('keydown',e=>{
        if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){ e.preventDefault(); preflight(); }
      });
    });
    /* 任务空间输入框：裸 Enter 就查。轮次链是只读查询，不涉及编译那种长任务，
       回车即跑符合直觉（这里没有 textarea 的换行语义要保护）。 */
    $('#gdir').addEventListener('keydown',e=>{
      if(e.key==='Enter'){ e.preventDefault(); chainOp('status'); }
    });
    /* ⌘/Ctrl+Enter 在三个输入框里都提交。裸 Enter 不提交 —— AC/非目标是
       textarea，裸 Enter 是换行（用户正要一条一条写判据），把它吃掉会毁掉输入。 */
    ['#gdmd','#gdac','#gdng'].forEach(sel=>{
      $(sel).addEventListener('keydown',e=>{
        if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){ e.preventDefault(); goalSubmit(); }
      });
    });
    document.querySelectorAll('.nav button').forEach(b=>
      b.addEventListener('click',()=>view(b.dataset.v)));
    document.querySelector('.nav').addEventListener('keydown',tabMove);
    $('#cmdk').addEventListener('click',cmdOpen);
    $('#cmd').addEventListener('click',e=>{ if(e.target.id==='cmd') cmdClose(); });
    $('#cmdq').addEventListener('input',cmdRun);
    document.addEventListener('keydown',e=>{
      const k=e.key;
      if((e.metaKey||e.ctrlKey)&&k.toLowerCase()==='k'){ e.preventDefault();
        $('#cmd').classList.contains('on')?cmdClose():cmdOpen(); return; }
      if($('#cmd').classList.contains('on')){
        if(k==='Escape'){ e.preventDefault(); cmdClose(); }
        else if(k==='ArrowDown'){ e.preventDefault(); cmdMove(1); }
        else if(k==='ArrowUp'){ e.preventDefault(); cmdMove(-1); }
        else if(k==='Enter'){ e.preventDefault(); cmdGo(); }
        else if(k==='Tab'){ e.preventDefault(); }     // 模态内不留逃逸口
        return;
      }
      if(k==='Escape') view('over');
      /* 数字键必须按**数值**比大小。原写法 k<=String(TABMAX) 是字符串比较：
         TABMAX 从 9 变 10（第 10 个视图「工作区」）后，'2'<='10' 为 false，
         于是 2..9 全部失配、实测键 2..8 一律停在 t-over。故 1..9 走数字键，
         第 10 个视图用 0（屏上 kbd 标签也写 0，两边不许各写一套）。 */
      else if(k>='0'&&k<='9'){
        const n=(k==='0')?10:+k;
        if(n<=TABMAX) view(Object.keys(TAB)[n-1]);
      }
      else if(k==='?') view('over');
    });
    view('over');
  },60);
}
// 徽章轮询：只碰 #fresh / #sbn / #ftfresh 三处，不重建任何视图。
// 单例注册，避免 boot() 被重复调用时叠出多个定时器。
if(!window.__freshTimer){
  window.__freshTimer=setInterval(freshTick,FRESH_TICK_MS);
  window.__agentos={D:D,view:view,cmdOpen:cmdOpen,freshTick:freshTick,   // 控制台可验
    wkRun:wkRun,wkHealth:wkHealth,wkSubmit:wkSubmit,wkParse:wkParse,
    wkAbort:wkAbort,wkStopBtn:wkStopBtn,
    hintLog:()=>WKHI.slice(),   // WKHI 是顶层 const，window.WKHI 拿不到，门只能走这里
    /* D77 目标台同样把顶层 const 通过 hook 递出去，否则门在页面里读不到
       GLAST / GRUN（顶层 let+const 不挂 window）。 */
    goalSubmit:goalSubmit,goalAbort:goalAbort,goalLoad:goalLoad,
    goalPlan:()=>GLAST,goalRun:()=>GRUN,goalHintLog:()=>GHI.slice(),
    preflight:preflight,preflightReset:preflightReset,preflightLast:()=>PLLAST,
    // D82 复盘台同样把顶层 let 通过 hook 递出去，否则门在页面里读不到 RLAST。
    retroLoad:retroLoad,retroReset:retroReset,retroLast:()=>RLAST};
}
function pdraw(j){
  const box=$('#gpre'); if(!box) return;
  if(!j||!j.ok||!Array.isArray(j.gates)||!j.gates.length){
    const m=(j&&(j.error||j.goal))||('rc='+((j&&j.rc)===undefined?'?':j.rc));
    box.innerHTML='<div class="gcard gblock"><h4>预检跑不起来'
      +'<span class="gst block">BLOCK</span></h4>'
      +`<p class="ggoal">${esc(m)}</p>`
      +'<div class="gmeta"><span>读不到盘就没有就绪度 —— 拿不到判据不给结论'
      +'（fail-closed）。台面不替你猜这轮能不能收。</span></div></div>';
    phint('BLOCK：'+m,'no'); return;
  }
  const ds=j.gates.filter(g=>g.kind==='disk'), dr=j.gates.filter(g=>g.kind==='draft');
  const nd=ds.filter(g=>!g.ok).length, nr=dr.filter(g=>!g.ok).length;
  box.innerHTML='<div class="gcard '+(j.ready?'gpass':'gblock')+'"><h4>'
    +('R'+esc(j.round)+' 收轮就绪度')
    +`<span class="gst ${j.ready?'pass':'block'}">${j.ready?'PASS 就绪':'BLOCK 还不能收'}</span>`
    +`<span class="gst">${esc(j.taskdir||'')}</span>`
    +`<span class="gst">目标 ${esc(j.state||'—')}</span>`
    +`<span class="gst">本轮 ${esc(j.rstate||'—')}</span></h4>`
    +`<h4>盘上判据<span class="gst">与输入无关 · ${ds.length-nd}/${ds.length} 过</span></h4>`
    +pglist(ds)
    +`<h4>你打的草案<span class="gst">引擎会怎么判 · ${dr.length-nr}/${dr.length} 过</span></h4>`
    +pglist(dr)
    +'<div class="gmeta">'
    +`<span><b>轮账</b> ${esc(j.taskdir||'')}/work/ROUNDS/R${esc(j.round)}.md</span>`
    +`<span><b>本轮账</b> ${esc(j.giga_run||'—')}</span>`
    +`<span><b>目标表 R${esc(j.round)}</b> ${esc(j.row_state||'—')}</span>`
    +`<span><b>动的 AC</b> ${esc((j.ac_moved||[]).join(',')||'—')}</span>`
    +`<span><b>未收轮</b> ${(j.pending||[]).length
        ?(j.pending||[]).map(r=>'R'+esc(r.n)+'('+esc(r.state||'—')+')').join(' '):'无'}</span>`
    +`<span><b>账目录</b> ${esc(j.gf_runs||'—')}`
    +`${j.gf_runs_from_env?'（来自 GF_OUT）':''}</span>`
    +`<span><b>耗时</b> ${esc(j.dur)}s</span>`
    +`<span><b>只读</b> ${j.readonly?'是（预检全程一个字节都没写）':'否'}</span></div></div>`
    +(j.close_argv?('<h4>就绪后在终端真跑的命令'
      +'<span class="gst">台面不代跑 —— 收轮要你亲手确认</span></h4>'
      +`<pre class="gcode" id="gpargv">${esc(j.close_argv.join(' \\\n  '))}</pre>`):'');
  phint(j.ready
    ?('R'+j.round+' 就绪 · 盘上 '+ds.length+' 条 + 草案 '+dr.length+' 条全过；'
      +'close-round 会真写盘，命令在上面，你自己跑')
    :('R'+j.round+' 还不能收 · 盘上红 '+nd+' 条 + 草案红 '+nr+' 条（逐条 why 在上面）'),
    j.ready?'ok':'no');
}
async function preflight(){
  if(PFBUSY){ phint('上一次预检还在跑 —— 等它回来','no'); return; }
  const dir=($('#gdir').value||'').trim();
  let n=parseInt(($('#gpn').value||''),10);
  if(!isFinite(n)) n=parseInt(($('#grn').value||''),10);
  if(!isFinite(n)) n=1;
  PFBUSY=true; $('#gcpf').disabled=true;
  phint('预检 R'+n+'…（只读盘，不 fork 引擎写面）');
  chlog('▶ close-preflight'+(dir?(' @ '+dir):' @ 活项目根')+' R'+n,'u');
  $('#gpre').innerHTML='';
  let j;
  try{
    const r=await fetch('/__goal/chain',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({op:'close-preflight',taskdir:dir,round:n,
        delta:$('#gpdl').value,evidence:$('#gpev').value,
        verdict:$('#gpvd').value,ac_moved:$('#gpa').value})});
    j=await r.json();
  }catch(e){ j={ok:false,rc:-1,verdict:'BLOCK',error:'NETWORK '+e.message,gates:[]}; }
  PFBUSY=false; $('#gcpf').disabled=false;
  chlog('◀ close-preflight rc='+(j.rc===undefined?'-':j.rc)
    +' 用时 '+(j.dur===undefined?'?':j.dur)+'s','u');
  PLLAST=j; pdraw(j);
}


// ── D82 复盘台：闭环最后一环「哪里做错了 / 为什么错 / 下次改什么」 ──────────
// 复发簇与自我能力计数全部来自 /__recurrence；后端复用引擎 evolution_intake 的
// load_events/build_clusters/RECURRENCE_MIN 与 postmortem.jsonl —— 台面不重算聚类、
// 不重写阈值、不自己判定失败。GET 只读，无需 CSRF 头。
// fail-closed：!j.ok 一律 BLOCK。读不到账本 != 没有失败。
let RLAST=null, RBUSY=false;
function rhint(txt,kind){
  const el=$('#rthint'); if(!el) return;
  el.innerHTML=txt?('<span class="'+(kind||'')+'">'+esc(txt)+'</span>'):'';
}
function retroReset(){
  RLAST=null; rhint('');
  const box=$('#rtbox'); if(!box) return;
  box.innerHTML='<div class="idle">未读。点「读复盘账本」；读不到就是读不到 —— '
    +'台面不会端出一份「没有失败」的假账本（fail-closed）。</div>';
}

/* ---- 工作区：文件 / 代码 / 终端三面 ----
   三面全部只读，且**失败一律照实说**：越界、沙箱拒、非白名单命令都把服务端
   的原话显示出来，不在台面上把它改写成一句「加载失败」。理由与 retroReset
   同源 —— 一个会自己编台面的环境面比没有环境面更危险。 */
let wsPath='', wsFile='', PROJ='', PROJF={};   // PROJ 是左栏选中的项目 key
let FPath='', FFile='', finited=false, tinited=false, sinited=false;
const SET={build:null,proj:null,health:null};   // R7 终端/文件/设置三面惰性状态

function wsEsc(s){
  return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;',
    '>':'&gt;','"':'&quot;'}[c]));
}

/* ── D96 项目空间 ──────────────────────────────────────────────
   左栏这一块的数据只来自 /__projects 的**真目录事实**（可读/条目/文件数/最新
   改动）。三条不许越过的线，都是这个项目一路在治的假绿在自己的身上复发：
     1) 读不到就写「读不到」，绝不摆两个占位项目充数；
     2) truncated 的文件数显示成「≥N」，不把「数到上限」写成精确值；
     3) 切换项目必须真换根（/__fs?proj=），不是换一段文案 —— 换文案叫装饰，
        而「看起来切了、其实还在读旧根」只有门能抓，所以 UI 侧也要留着
        当前真实根路径给用户核对。 */
function projLabel(){
  const p=(PROJF.projects||[]).find(x=>x.key===PROJ);
  return p?p.label:(PROJ||'未选项目');
}
function projPath(){
  const p=(PROJF.projects||[]).find(x=>x.key===PROJ);
  return p?p.root:'（项目未知）';
}
function projPathSet(){
  const el=$('#wsproj'); if(!el) return;
  el.textContent=projPath(); el.title=projPath();
}
function projCount(f){
  const n=f.truncated?('≥'+f.n_files):String(f.n_files);
  return n+' 文件';
}
function projInit(){
  const nav=$('#projspace'); if(!nav) return;
  fetch('/__projects').then(r=>r.json()).then(j=>{
    PROJF=j||{};
    if(!j.ok||!(j.projects||[]).length){
      nav.innerHTML='<p class="pjerr">项目空间读不到：'
        +wsEsc(j.error||'服务端没给理由')+'</p>';
      return;
    }
    if(!PROJ||!j.projects.some(x=>x.key===PROJ)) PROJ=j.active||j.default||'';
    nav.innerHTML=j.projects.map(p=>{
      const f=p.facts||{};
      return '<button type="button" class="pjnode" data-proj="'+wsEsc(p.key)+'"'
        +' aria-current="'+String(p.key===PROJ)+'"'
        +' title="'+wsEsc(p.role)+'&#10;'+wsEsc(p.root)+'"><span class="r">'+wsEsc(p.label)
        /* 徽章放 key 而不是文件数：文件数下一行已经有一份，徽章再放一次既是重复，
           又让读屏把它念成「驾驶舱 714」这种没有单位的名词。key 是接口面上真实
           的名字，放这里用户能拿它去对照 /__fs?proj= 的参数。 */
        +'<span class="b">'+wsEsc(p.key)+'</span></span>'
        +'<span class="p">'+wsEsc(p.root)+'</span>'
        +'<span class="n'+((f.readable?'':' bad'))+'">'
        +wsEsc(f.readable?(projCount(f)+' · 顶层 '+f.n_top):('不可读：'+(f.error||'')))
        +'</span></button>';
    }).join('');
    nav.querySelectorAll('button[data-proj]').forEach(b=>
      b.addEventListener('click',()=>projPick(b.dataset.proj)));
  }).catch(e=>{
    nav.innerHTML='<p class="pjerr">项目空间读不到：'+wsEsc(e.message)+'</p>';
  });
}
function projPick(key){
  if(!key||key===PROJ) return;
  PROJ=key;
  document.querySelectorAll('#projspace .pjnode').forEach(b=>
    b.setAttribute('aria-current',String(b.dataset.proj===PROJ)));
  // 换根必须把上一次的路径与文件清掉：旧根里的相对路径在新根里指向别的东西，
  // 留着会让用户以为自己还在看刚才那个目录 —— 换根不清状态是这类面板最常见的
  // 骗人形状。
  wsPath=''; wsFile='';
  $('#wscode').textContent='已切到'+projLabel()+'，选一个文件看内容。';
  $('#wsdct').textContent='读 /__fs?proj='+PROJ+'&op=read';
  $('#wspath').textContent='.';
  projPathSet();
  // 文件面与工作区同一个换根清零口径：旧根里的相对路径在新根里指向别的东西，
  // 留着会让用户以为自己还在看刚才那个目录。已初始化过就重拉，不依赖它此刻
  // 是否可见 —— 惰性初始化只会发生一次，回来时不等重拉就会拿着旧根的树。
  FPath=''; FFile='';
  const fcode=$('#fcode');
  if(fcode){ fcode.textContent='已切到'+projLabel()+'，选一个文件看内容。'; fcode.classList.add('idle'); }
  const fdct=$('#fdct'); if(fdct) fdct.textContent='读 /__fs?op=read';
  const fpath=$('#fpath'); if(fpath) fpath.textContent='.';
  fileProjSet();
  if($('#main')&&$('#v-ws').classList.contains('on')) wsList('');
  if(finited) fileList('');
}
async function wsList(path){
  const tree=$('#wstree'); if(!tree) return;
  let j;
  try{
    const r=await fetch('/__fs?proj='+encodeURIComponent(PROJ)
      +'&op=list&depth=1&path='+encodeURIComponent(path||''));
    j=await r.json();
  }catch(e){
    tree.innerHTML='<li><div class="idle">读不到目录：'+wsEsc(e.message)+'</div></li>';
    return;
  }
  if(!j.ok){ tree.innerHTML='<li><div class="idle">'+wsEsc(j.error)+'</div></li>'; return; }
  wsPath=j.path||'.';
  $('#wspath').textContent=wsPath;
  projPathSet();
  if(!j.entries.length){
    tree.innerHTML='<li><div class="idle">这个目录没有可显示的条目。</div></li>';
    return;
  }
  let h='';
  if(wsPath!=='.'){
    const up=wsPath.split('/').slice(0,-1).join('/');
    h+='<li><button type="button" data-p="'+wsEsc(up)+'">'
      +'<span class="d">..</span><span>上一层</span></button></li>';
  }
  for(const e of j.entries){
    h+='<li><button type="button" data-p="'+wsEsc(e.path)+'" data-d="'
      +(e.dir?'1':'0')+'"'+(e.path===wsFile?' aria-current="true"':'')+'>'
      +'<span class="d">'+(e.dir?'▸':'·')+'</span><span>'+wsEsc(e.name)+'</span>'
      +(e.dir?'':'<span class="sz">'+e.size+'B</span>')+'</button></li>';
  }
  tree.innerHTML=h;
}

async function wsRead(path){
  const box=$('#wscode'); if(!box) return;
  box.textContent='读取中…';
  let j;
  try{
    const r=await fetch('/__fs?proj='+encodeURIComponent(PROJ)
      +'&op=read&path='+encodeURIComponent(path));
    j=await r.json();
  }catch(e){
    box.textContent='读不到文件：'+e.message; return;
  }
  if(!j.ok){ box.textContent=j.error||'读不到（服务端没给理由）'; return; }
  wsFile=path;
  $('#wsdct').textContent=j.path+' · '+projLabel()+' · '+j.n_lines+' 行 · '+j.size+'B'
    +(j.truncated?' · 已截断到 '+j.limit+'B（不是全文）':'');
  box.textContent=j.text;
}

async function wsTerm(cmd){
  const box=$('#wsterm'); if(!box) return;
  const argv=String(cmd||'').trim().split(/\s+/).filter(Boolean);
  if(!argv.length) return;
  box.textContent='$ '+argv.join(' ')+'\n运行中…';
  let j;
  try{
    const r=await fetch('/__term',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({argv})});
    j=await r.json();
  }catch(e){
    box.textContent='$ '+argv.join(' ')+'\n请求失败：'+e.message; return;
  }
  if(!j.ok){ box.textContent='$ '+argv.join(' ')+'\n'+j.error; return; }
  const rcTxt=(j.rc===0?'0':(j.rc==null?'超时':String(j.rc)));
  box.innerHTML='<span class="d">$ '+wsEsc(argv.join(' '))+'</span>\n'
    +wsEsc(j.out||'(无输出)')+'\n<span class="rc" data-rc="'+wsEsc(rcTxt)+'">'
    +'rc='+wsEsc(rcTxt)+'</span> · '+j.dur+'s'
    +(j.truncated?' · 输出已截断':'');
}

/* 浏览器面：把 URL 交给后端 /__web 取字节，如实报回事实。纪律与代码/终端
   两面同一条 —— 失败一律显示服务端原话，不改写成一句「加载失败」。
   预览那一步只把取回的 text 塞进 sandbox iframe 的 srcdoc，绝不写 iframe.src：
   写了就是给一份任意本机页面发同源能力。 */
/* ── 右栏：上下文与执行状态（D88）─────────────────────────────
   三条规矩，写在代码里而不是文档里，因为它们都是可被违反的：
   1. 每个字段都来自一次真实读取（/__health 或内联 data.json）。
      读不到就写「不可判定」，绝不填一个好看的默认值 —— 一个填出来的
      「clients 1」和真的只有一个客户端，在屏上长得一模一样。
   2. 上次动作的 rc 是执行面自己记的账（/__health.act_last.rc），
      前端不自己推断、不自己重算。
   3. code_sha 单独列出来。它和盘上源码不一致时，页面其余部分全绿也
      可能是在跑昨天那份代码 —— 这是 v118 实测过的真实盲区。 */
let CXH=null, CXT=null;
/* 总闸那一组是内联的（页面出生时带着 data.json），所以它不依赖 /__health：
   常驻实例读不到时这一组照样是真值。反过来它永远是快照，所以屏上必须
   标明它是快照而不是实时，否则用户会把 generated_at 当成「此刻」。 */
function cxGate(){
  const gc=D.gate_counts||{}, cov=D.coverage||{}, ag=D.aggregate||{};
  const eg=Array.isArray(D.env_gated)?D.env_gated:[];
  const gtxt=Object.keys(gc).map(k=>k+' '+gc[k]).join(' · ')||'—';
  const gv=D.global_verdict||null;
  cxRows($('#cxgate'),[
    ['总闸结论',gv,gv==='PASS'?'ok':(gv?'no':'dim')],
    ['门统计',gtxt,'dim'],
    ['判定分歧',(D.n_divergence==null?'—':String(D.n_divergence)),'dim'],
    ['覆盖行数',(cov.n_rows==null?'—':cov.n_rows+' / '+(cov.n_expected==null?'?':cov.n_expected)),
      (cov.coverage_complete?'ok':'no')],
    ['缺阶段',((cov.missing_dirs||[]).length?cov.missing_dirs.join(' '):'无'),'dim'],
    ['已执行',(ag.N_EXECUTED==null?'—':String(ag.N_EXECUTED)+' / '+(ag.n_dirs==null?'?':ag.n_dirs)),'dim'],
    ['环境受限',(eg.length?eg.map(x=>x.dir).join(' '):'无'),eg.length?'no':'dim'],
    ['快照时间',fmtT(D.generated_at),'dim']
  ]);
  $('#cxgatenote').textContent='这一组是页面内联快照（生成于 '+fmtT(D.generated_at)
    +'），不是实时查询；上面两组才是每次轮询 /__health 的实时值。';
}
function cxRows(box,rows){
  if(!box) return;
  box.innerHTML=rows.map(r=>'<div class="cxr"><dt>'+esc(r[0])+'</dt><dd'
    +(r[2]?' class="'+r[2]+'"':'')+'>'+(r[1]==null?'不可判定':esc(r[1]))+'</dd></div>').join('');
}
function cxDraw(){
  if(!CXH){                                    /* /__health 读不到：全部不可判定 */
    const run=$('#cxrun');
    if(run){ run.dataset.r='idle';
      $('#cxrunval').textContent='不可判定（/__health 读不到）';
      $('#cxrunval').className='val'; }
    $('#cxexecn').textContent='读 /__health 失败';
    cxRows($('#cxact'),[['当前动作','不可判定','dim'],['上次动作','不可判定','dim'],
      ['上次 rc','不可判定','dim'],['中止次数','不可判定','dim']]);
    $('#cxactnote').textContent='常驻实例读不到，执行状态整栏不可判定 —— 不拿上一轮快照充数。';
    cxRows($('#cxinst'),[['clients','不可判定','dim'],['code_sha','不可判定','dim'],
      ['启动时间','不可判定','dim'],['重建次数','不可判定','dim'],['末次构建','不可判定','dim']]);
    $('#cxinstnote').textContent='这一栏最要紧的一格是 code_sha：它对不上，屏上其余全绿也可能是旧代码。';
    return;
  }
  /* 「正在跑哪个动作」只有执行面知道 —— 它是服务端在 __health 里报的
     act_running。本页的 actBusy 只表示「**这个标签页**正在发一个动作」，
     拿它冒充全局执行状态会在多标签页时直接说错，所以不参与。 */
  const h=CXH, last=h.act_last||null, run=h.act_running||null;
  const r=$('#cxrun');
  if(r){
    r.dataset.r=run?'run':(last?(last.rc===0?'ok':'no'):'idle');
    const v=$('#cxrunval');
    v.textContent=run?('正在跑 '+run)
      :(last?(last.action+' rc='+last.rc):'空闲（无动作记录）');
    v.className='val'+(run?'':(last?(last.rc===0?' ok':' no'):''));
  }
  $('#cxexecn').textContent='读 /__health · '+fmtT(new Date().toISOString());
  cxRows($('#cxact'),[
    ['当前动作',run,'dim'],
    ['上次动作',last?last.action:null,'dim'],
    ['上次 rc',last?String(last.rc):null,last?(last.rc===0?'ok':'no'):'dim'],
    ['上次耗时',last?(last.dur+'s'):null,'dim'],
    ['本进程执行数',(h.act_run==null?'—':String(h.act_run)),'dim'],
    ['被用户中止',(h.act_aborted==null?'—':String(h.act_aborted)),'dim']
  ]);
  $('#cxactnote').textContent='rc 是执行面自己记的，不是本页推断的。';
  const st=h.stats||{};
  cxRows($('#cxinst'),[
    ['clients',(h.clients==null?'—':String(h.clients)),'dim'],
    ['code_sha',h.code_sha?String(h.code_sha).slice(0,12):null,'dim'],
    ['启动时间',st.started,'dim'],
    ['重建次数',st.rebuilds==null?'—':String(st.rebuilds),'dim'],
    ['末次构建',st.last_build||'本次未重建','dim']
  ]);
  $('#cxinstnote').textContent='code_sha 与盘上 tools/serve.py 的 sha 不一致时，'
    +'页面全绿也可能在跑旧代码（v118 实测过的盲区）。';
}

async function cxHealth(){
  try{
    const r=await fetch('/__health',{cache:'no-store'});
    CXH=await r.json();
  }catch(e){ CXH=null; }
  cxDraw();
}

async function wsWeb(raw){
  const box=$('#wswebstat'), fr=$('#wsprev');
  if(!box) return;
  const url=String(raw||'').trim();
  if(!url){ box.innerHTML='未填地址，没发请求。'; return; }
  box.innerHTML='取回中…（后端最多等 8s）';
  fr.removeAttribute('srcdoc');
  let j;
  try{
    const r=await fetch('/__web?url='+encodeURIComponent(url));
    j=await r.json();
  }catch(e){
    box.innerHTML='<span class="no">请求失败：'+wsEsc(e.message)+'</span>'; return;
  }
  if(!j.ok){
    const code=(j.status?'HTTP '+wsEsc(String(j.status))+' · ':'');
    box.innerHTML='<span class="no">'+code+wsEsc(j.error||'取回失败（服务端没给理由）')+'</span>';
    return;
  }
  box.innerHTML='<span class="ok">'+wsEsc(String(j.status))+' '+wsEsc(j.reason||'')+'</span>'
    +' · '+wsEsc(j.content_type)+' · '+wsEsc(String(j.bytes))+'B · '
    +wsEsc(String(j.dur))+'s'
    +(j.truncated?'<span class="no"> · 已截断到 '+wsEsc(String(j.limit))+'B（不是全文）</span>':'')
    +'<br>来自 '+wsEsc(j.url);
  if(j.text!=null) fr.setAttribute('srcdoc',j.text);
  else box.innerHTML+='<br><span class="no">没有可渲染的文本（这一面只渲染文本类型）。</span>';
}

function wsInit(){
  const tree=$('#wstree'); if(!tree) return;
  tree.addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-p]'); if(!b) return;
    if(b.dataset.d==='1') wsList(b.dataset.p);
    else wsRead(b.dataset.p);
  });
  $('#wsroot').addEventListener('click',()=>wsList(''));
  $('#wsform').addEventListener('submit',ev=>{
    ev.preventDefault();
    const q=$('#wsq'); wsTerm(q.value); q.value='';
  });
  $('#wsrun').addEventListener('click',()=>{
    const q=$('#wsq'); wsTerm(q.value); q.value='';
  });
  $('#wsquick').addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-cmd]'); if(!b) return;
    wsTerm(b.dataset.cmd);
  });
  $('#wswebform').addEventListener('submit',ev=>{
    ev.preventDefault();
    const u=$('#wswebu'); wsWeb(u.value); u.value='';
  });
  $('#wswebgo').addEventListener('click',()=>{
    const u=$('#wswebu'); wsWeb(u.value); u.value='';
  });
}


/* ── R7 新视图：终端 / 文件 / 设置 ──────────────────────────────
   三面全只读，失败一律把服务端原话写进 DOM（与 ws 三条同纪律：读不到就写读不到，
   绝不摆占位数据充数）。终端复用同一 /__term 只读白名单路由；文件复用 /__fs 与
   /__projects；设置逐行标来源 —— 每行的值都来自一次真 fetch 或页面内联快照。 */
async function termRun(cmd){
  const box=$('#tout'); if(!box) return;
  const argv=String(cmd||'').trim().split(/\s+/).filter(Boolean);
  if(!argv.length) return;
  box.textContent='$ '+argv.join(' ')+'\n运行中…';
  let j;
  try{
    const r=await fetch('/__term',{method:'POST',
      headers:{'Content-Type':'application/json','X-AgentOS-Action':'1'},
      body:JSON.stringify({argv})});
    j=await r.json();
  }catch(e){
    box.textContent='$ '+argv.join(' ')+'\n请求失败：'+e.message; return;
  }
  if(!j.ok){ box.textContent='$ '+argv.join(' ')+'\n'+j.error; return; }
  const rcTxt=(j.rc===0?'0':(j.rc==null?'超时':String(j.rc)));
  box.innerHTML='<span class="d">$ '+wsEsc(argv.join(' '))+'</span>\n'
    +wsEsc(j.out||'（无输出）')+'\n<span class="rc" data-rc="'+wsEsc(rcTxt)+'">'
    +'rc='+wsEsc(rcTxt)+'</span> · '+j.dur+'s'
    +(j.truncated?' · 输出已截断':'');
  const c=$('#tct'); if(c) c.textContent='POST /__term · rc='+wsEsc(rcTxt)+' · '+j.dur+'s';
}
function termInit(){
  tinited=true;
  const form=$('#tform'); if(!form) return;
  form.addEventListener('submit',ev=>{
    ev.preventDefault(); const q=$('#tq'); termRun(q.value); q.value='';
  });
  $('#trun').addEventListener('click',()=>{
    const q=$('#tq'); termRun(q.value); q.value='';
  });
  $('#tquick').addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-cmd]'); if(!b) return;
    termRun(b.dataset.cmd);
  });
}
function fileProjSet(){
  const el=$('#fproj'); if(!el) return;
  el.textContent=projPath(); el.title=projPath();
}
async function fileEnsureProj(){
  if(PROJ&&PROJF&&PROJF.projects&&PROJF.projects.length) return true;
  let j;
  try{ j=await (await fetch('/__projects')).json(); }
  catch(e){ $('#fpath').textContent='读不到项目：'+e.message; return false; }
  PROJF=j||{};
  if(!j.ok||!(j.projects||[]).length){
    $('#fpath').textContent='读不到项目：'+(j.error||'服务端没给理由');
    return false;
  }
  if(!PROJ||!j.projects.some(x=>x.key===PROJ)) PROJ=j.active||j.default||'';
  return true;
}
async function fileInit(){
  finited=true;
  const tree=$('#ftree'); if(!tree) return;
  tree.addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-p]'); if(!b) return;
    if(b.dataset.d==='1') fileList(b.dataset.p); else fileRead(b.dataset.p);
  });
  $('#froot').addEventListener('click',()=>fileList(''));
  if(await fileEnsureProj()) fileList('');
}
async function fileList(path){
  const tree=$('#ftree'); if(!tree) return;
  let j;
  $('#fpath').textContent='读取中…'; fileProjSet();
  try{
    const r=await fetch('/__fs?proj='+encodeURIComponent(PROJ)
      +'&op=list&depth=1&path='+encodeURIComponent(path||''));
    j=await r.json();
  }catch(e){
    tree.innerHTML='<li><div class="idle">读不到目录：'+wsEsc(e.message)+'</div></li>';
    $('#fpath').textContent='读不到目录'; return;
  }
  if(!j.ok){
    tree.innerHTML='<li><div class="idle">'+wsEsc(j.error)+'</div></li>';
    $('#fpath').textContent=j.error||'读不到'; return;
  }
  FPath=j.path||'.'; FFile='';
  $('#fpath').textContent=FPath; fileProjSet();
  if(!j.entries.length){
    tree.innerHTML='<li><div class="idle">这个目录没有可列出的条目。</div></li>';
    return;
  }
  let h='';
  if(FPath!=='.'){
    const up=FPath.split('/').slice(0,-1).join('/');
    h+='<li><button type="button" data-p="'+wsEsc(up)+'">'
      +'<span class="d">..</span><span>上一层</span></button></li>';
  }
  for(const e of j.entries){
    h+='<li><button type="button" data-p="'+wsEsc(e.path)+'" data-d="'
      +(e.dir?'1':'0')+'"'+(e.path===FFile?' aria-current="true"':'')+'>'
      +'<span class="d">'+(e.dir?'▸':'·')+'</span><span>'+wsEsc(e.name)+'</span>'
      +(e.dir?'':'<span class="sz">'+e.size+'B</span>')+'</button></li>';
  }
  tree.innerHTML=h;
}
async function fileRead(path){
  const box=$('#fcode'); if(!box) return;
  box.textContent='读取中…';
  let j;
  try{
    const r=await fetch('/__fs?proj='+encodeURIComponent(PROJ)
      +'&op=read&path='+encodeURIComponent(path));
    j=await r.json();
  }catch(e){ box.textContent='读不到文件：'+e.message; return; }
  if(!j.ok){ box.textContent=j.error||'读不到（服务端没给理由）'; return; }
  FFile=path;
  $('#fdct').textContent=j.path+' · '+projLabel()+' · '+j.n_lines+' 行 · '+j.size+'B'
    +(j.truncated?' · 已截断到 '+j.limit+'B（不是全文）':'');
  box.textContent=j.text;
  box.classList.remove('idle');   // leaves idle only after real content arrives
  document.querySelectorAll('#ftree button[data-p]').forEach(
    b=>b.setAttribute('aria-current',String(b.dataset.p===path)));
}
async function setFetchBuild(){
  try{
    const r=await fetch('/__fs?op=read&path=work/build.json');
    const j=await r.json();
    if(!j.ok) return {error:j.error||'读不到'};
    try{ return {ok:true,data:JSON.parse(j.text)}; }
    catch(e){ return {error:'build.json 解析失败：'+e.message}; }
  }catch(e){ return {error:e.message}; }
}
async function setFetchProj(){
  try{
    const j=await (await fetch('/__projects')).json();
    if(!j.ok) return {error:j.error||'读不到'};
    return {ok:true,data:j};
  }catch(e){ return {error:e.message}; }
}
async function setFetchHealth(){
  try{
    const j=await (await fetch('/__health')).json();
    if(!j.ok) return {error:j.error||'读不到'};
    return {ok:true,data:j};
  }catch(e){ return {error:e.message}; }
}
function setRow(l,v,src,cls){
  return '<div class="setr"><dt>'+wsEsc(l)+'</dt><dd'+(cls?' class="'+cls+'"':'')+'>'
    +wsEsc(v)+'<span class="setsrc">'+wsEsc(src)+'</span></dd></div>';
}
function setFill(){
  const box=$('#setmeta'); if(!box) return;
  const rows=[];
  rows.push(setRow('服务原点',location.origin,'浏览器 location.origin'));
  rows.push(setRow('内联快照时间',fmtT(D.generated_at),'data.json 内联（非实时）'));
  rows.push(setRow('视图 / tab 数',String(Object.keys(TAB).length),'JS TAB（与 tablist 同源）'));
  const b=SET.build;
  if(b&&b.ok&&b.data){ const d=b.data;
    rows.push(setRow('构建时间',d.built_at||'—','build.json built_at'));
    rows.push(setRow('data_sha256',d.data_sha256||'—','build.json data_sha256'));
    rows.push(setRow('html_sha256',d.html_sha256||'—','build.json html_sha256'));
    rows.push(setRow('file_sha256',d.file_sha256||'—','build.json file_sha256'));
  } else if(b&&b.error){
    rows.push(setRow('构建指纹','读不到：'+b.error,'build.json（/__fs）','no'));
  } else { rows.push(setRow('构建指纹','（未读）','build.json（/__fs）')); }
  const p=SET.proj;
  if(p&&p.ok&&p.data){
    const j=p.data; const key=j.active||j.default||'';
    const proj=(j.projects||[]).find(x=>x.key===key)||{};
    rows.push(setRow('当前项目',key?(key+' · '+(proj.label||'')):'未选',
      '/__projects active/default'));
    rows.push(setRow('项目根路径',proj.root||'（未知）','/__projects projects.root'));
  } else if(p&&p.error){
    rows.push(setRow('项目','读不到：'+p.error,'/__projects','no'));
  } else { rows.push(setRow('项目','（未读）','/__projects')); }
  const h=SET.health;
  if(h&&h.ok&&h.data){ const j=h.data;
    rows.push(setRow('常驻 clients',String(j.clients==null?'—':j.clients),
      '/__health clients'));
    rows.push(setRow('常驻启动',((j.stats||{}).started)||'—','/__health stats.started'));
    rows.push(setRow('服务端代码指纹',j.index||'—','/__health index'));
    rows.push(setRow('服务端数据指纹',j.data||'—','/__health data'));
    if(SET.build&&SET.build.ok&&SET.build.data){
      const live=String(j.index||'').split(':')[0];
      const built=String(SET.build.data.file_sha256||'').slice(0,12);
      const same=(!!live&&!!built&&live===built);
      rows.push(setRow('代码指纹一致',
        same?'一致（前 12 位相同）':'不一致或读不全',
        '交叉核对 /__health × build.json',same?'ok':'no'));
    }
  } else if(h&&h.error){
    rows.push(setRow('运行态','读不到：'+h.error,'/__health','no'));
  } else { rows.push(setRow('运行态','（未读）','/__health')); }
  box.innerHTML=rows.join('');
}
async function setRefresh(what){
  const want=(what==='all')?['build','proj','health']:[what];
  for(const w of want){
    if(w==='build') SET.build=await setFetchBuild();
    else if(w==='proj') SET.proj=await setFetchProj();
    else if(w==='health') SET.health=await setFetchHealth();
  }
  setFill();
}
function setInit(){
  sinited=true;
  $('#setre-build').addEventListener('click',()=>setRefresh('build'));
  $('#setre-proj').addEventListener('click',()=>setRefresh('proj'));
  $('#setre-health').addEventListener('click',()=>setRefresh('health'));
  $('#setre-all').addEventListener('click',()=>setRefresh('all'));
  setFill(); setRefresh('all');
}

function rclusters(j){
  const cl=j.clusters||[], thr=j.threshold, nr=j.n_recurring||0;
  const head='<div class="gcard '+(nr?'gblock':'gpass')+'"><h4>复发簇'
    +'<span class="gst '+(nr?'block':'pass')+'">'
    +(nr?(nr+'/'+j.n_clusters+' 个能力缺口还没修好'):'暂无复发')
    +'</span><span class="gst">阈值 runs≥'+(thr==null?'—':esc(thr))+'</span>'
    +'<span class="gst">取自引擎 RECURRENCE_MIN</span></h4>';
  if(!cl.length){
    return head+'<p class="ggoal">没有簇。events='+esc(j.n_events)
      +' —— 事件为 0 不构成「没失败」的证据，只是没有可聚类的复发样本。</p></div>';
  }
  const items=cl.map(c=>{
    const bad=c.recurring?1:0;
    return '<li data-ok="'+bad+'">'
      +'<span class="gn">'+esc(c.runs)+' run</span>'
      +'<span class="gst '+(bad?'block':'pass')+'">'+(bad?'未修好':'未达阈')+'</span>'
      +'<span class="gw"><b>'+esc(c.cluster)+'</b>'
      +'<span class="gwhy">'+esc(c.family||'—')+' · events '+esc(c.events)
      +' · 最后一次 '+esc(c.last_ts||'—')+'</span>'
      +(c.plan?'<span class="gwhy"><b>下次改什么：</b>'+esc(c.plan)+'</span>':'')
      +'</span></li>';
  }).join('');
  return head+'<ul class="glist">'+items+'</ul>'
    +'<div class="gmeta"><span><b>读法</b> 达阈值 = 同一类错在 '+esc(thr)
      +' 个以上 run 里反复出现，也就是这个能力缺口还没被修好</span>'
    +'<span><b>总事件</b> '+esc(j.n_events)+'</span>'
    +'<span><b>总簇</b> '+esc(j.n_clusters)+'</span></div></div>';
}

// 桶大小极不均匀（最大 80 run，最小 2 run）。所以只给绝对计数 + 每 run 均值，
// 不给「趋势斜率」结论，并把每桶 run 数一起列出 —— 否则一个 2-run 的小桶会被
// 读成「已经变强」。
function rtrend(tr){
  if(!tr||!tr.length) return '';
  const rows=tr.map(b=>{
    const per=(k)=>b.n?(Math.round(b[k]*10/b.n)/10):'0';
    return '<tr><td class="gn-cell">'+esc(b.day)+'</td>'
      +'<td>'+esc(b.n)+'</td><td>'+esc(b.ver)+'</td>'
      +'<td>'+esc(b.rep)+'</td><td>'+esc(b.rec)+'</td>'
      +'<td class="gn-cell">'+per('ver')+' / '+per('rep')+' / '+per('rec')+'</td></tr>';
  }).join('');
  return '<table class="gtbl"><caption class="sr">按天的自我诊断计数与每 run 均值</caption>'
    +'<thead><tr><th>日期</th><th>run</th><th>自验证</th><th>自修复</th>'
    +'<th>自恢复</th><th>每 run 均值（验/修/复）</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

function rself(j){
  const s=j.self||{}, v=s.verdicts||{}, tr=j.trend||[];
  const hi=s.human_interventions||0, ht=s.human_takeovers||0;
  const y=(k)=>'<span><b>'+esc(k)+'</b> '+esc(s[k])+'</span>';
  return '<div class="gcard"><h4>自我诊断计数'
    +'<span class="gst">runs '+esc(s.runs)+'</span>'
    +'<span class="gst">BLOCK '+esc(v.BLOCK||0)
    +' · SHIP_OK '+esc(v.SHIP_OK||0)+'</span></h4>'
    +'<div class="gmeta">'+y('agent_self_verifications')+y('agent_self_repairs')
    +y('agent_self_recoveries')
    +'<span><b>可泛化</b> '+esc(s.n_generalizable)+'/'+esc(s.runs)+'</span>'
    +'<span><b>有改进建议</b> '+esc(s.n_with_plan)+'/'+esc(s.runs)+'</span>'
    +'<span><b>根因已分类</b> '+esc(s.n_family_known)+'/'+esc(s.runs)
    +'（其余 UNKNOWN_SEMANTIC，如实显示，不编家族）</span>'
    +'<span><b>人类介入</b> '+esc(hi)+' · <b>人类接管</b> '+esc(ht)+'</span></div>'
    +rhonest(hi,ht)+rtrend(tr)+'</div>';
}

// 诚实纪律：人类侧计数在真实数据里长期为 0。常量 0 画成「下降斜线」就是造假，
// 所以这里只照实给出原值并说明为什么不画趋势；趋势只取 agent 侧真有分化的三个计数。
function rhonest(hi,ht){
  if(hi!==0||ht!==0) return '';
  return '<p class="note" style="margin:8px 0 0">人类侧两个计数在真实数据里全程为 0，'
    +'所以这里不画「人类干预在下降」的趋势线。趋势只取 agent 侧真有分化的三个计数。</p>';
}

function rledger(j){
  const L=j.ledger||[];
  const head='<div class="gcard"><h4>复盘账本<span class="gst">新 → 旧 '+L.length
    +' 条</span><span class="gst">逐字取自引擎 postmortem.jsonl</span></h4>';
  if(!L.length){
    return head+'<p class="ggoal">账本为空 —— 这是「没读到」，不是「没有失败」。</p></div>';
  }
  const items=L.map(r=>{
    const ok=r.verdict==='SHIP_OK'?1:0;
    const tf=(v)=>(v===null||v===undefined)?'—':String(v);
    return '<li data-ok="'+ok+'">'
      +'<span class="gn">'+esc((r.ts||'').slice(5,16))+'</span>'
      +'<span class="gst '+(ok?'pass':'block')+'">'+esc(r.verdict)+'</span>'
      +'<span class="gw"><b>'+esc(r.gap||'—')+'</b>'
      +'<span class="gwhy"><b>为什么错：</b>'+esc(r.root_cause||'—')
        +'（'+esc(r.family||'UNKNOWN_SEMANTIC')+'）</span>'
      +'<span class="gwhy"><b>下次改什么：</b>'+esc(r.plan||'—')+'</span>'
      +'<span class="gwhy">可泛化 '+(r.generalizable?'是':'否')
        +' · 可恢复 '+esc(tf(r.recoverable))
        +' · 可预防 '+esc(tf(r.preventable))+'</span>'
      +'</span></li>';
  }).join('');
  return head+'<ul class="glist">'+items+'</ul>'
    +'<div class="gmeta"><span><b>账本</b> '+esc(j.memory||'—')+'</span>'
    +'<span><b>只读</b> 台面一个字节都没写</span></div></div>';
}

function rdraw(j){
  const box=$('#rtbox'); if(!box) return;
  if(!j||!j.ok){
    const m=(j&&j.error)||'/__recurrence 不可达';
    box.innerHTML='<div class="gcard gblock"><h4>复盘账本读不出来'
      +'<span class="gst block">BLOCK</span></h4>'
      +'<p class="ggoal">'+esc(m)+'</p>'
      +'<div class="gmeta"><span>读不到账本就不给「没有复发 / 已经在变强」这种结论 —— '
      +'拿不到判据不给就绪度（fail-closed）。台面不替你猜。</span></div></div>';
    rhint('BLOCK：'+m,'no'); return;
  }
  box.innerHTML=rclusters(j)+rself(j)+rledger(j);
  rhint('已读 · '+j.n_clusters+' 簇（达阈值 '+(j.n_recurring||0)+'）'
    +' · 账本 '+((j.ledger||[]).length)+' 条 · '+((j.trend||[]).length)+' 天趋势','ok');
}
async function retroLoad(){
  if(RBUSY){ rhint('上一次读账本还在跑 —— 等它回来','no'); return; }
  RBUSY=true; const b=$('#rtgo'); if(b) b.disabled=true;
  rhint('读复盘账本…（只读 postmortem.jsonl，不写盘）');
  let j;
  try{
    const r=await fetch('/__recurrence');
    j=await r.json();
  }catch(e){ j={ok:false,error:'NETWORK '+e.message}; }
  RBUSY=false; if(b) b.disabled=false;
  RLAST=j; rdraw(j);
}

/* ---- 能力地图：Agent 攒下了什么，其中多少被**独立证据**验过 ----
   这一面最容易被做成一张漂亮假账。账本里 status=PROMOTED 有 26 条，
   但独立 A/B 裁决里 PASS=0。所以屏上**永远并排**「自报」与「验过」，
   绝不合成一个「已晋升 26」的单一数字 —— 那个数字会把 26 条未验证的账
   一次包装成 26 份能力。四桶互斥且穷尽（判据在 serve.py CAP_VERIFIED_VERDICTS）：
     ab-verified / ab-unverifiable / no-verdict / anonymous
   fail-closed：端点不可达或 503 一律 BLOCK，不给「没有能力 / 一切正常」这种结论。 */
let CAPLAST=null, CAPBUSY=false;
const CAP_BUCKETS=[
  {k:'ab-verified',     t:'已验过 · 拿得出 A/B 裁决',
   d:'有独立 A/B 裁决，且裁决落在 HOLD-AB / HOLD-EXIST'},
  {k:'ab-unverifiable', t:'A/B 验不了 · 引擎自报',
   d:'引擎自己判定 UNVERIFIABLE —— 不是失败，是判据不足'},
  {k:'no-verdict',      t:'没跑过 A/B · 连裁决都没有',
   d:'有名字、有状态，但一次独立 A/B 裁决都没有'},
  {k:'anonymous',       t:'无身份 · 连 capability_id 都没有',
   d:'无法追溯的账：既不能证明做成过，也不能证明没做'}];
function caphint(txt,kind){
  const el=$('#caphint'); if(!el) return;
  el.innerHTML=txt?('<span class="'+(kind||'')+'">'+esc(txt)+'</span>'):'';
}
/* 进屏即「未读」。这一面最容易被做成一张默认满屏的漂亮假账：
   四张桶卡 + 一条整齐的「已验过 20」摆在首屏，用户还没点任何按钮就以为
   系统已经证明自己有 20 项能力。所以进屏只有一句话和零个数字。 */
function capReset(){
  CAPLAST=null; caphint('');
  const bb=$('#capbuckets'), ls=$('#caplist'), av=$('#capav');
  const idle='<div class="idle">未读。点「读能力地图」；读不到就是读不到 —— '
    +'台面不会先端出一份整齐的能力账来充数（fail-closed）。</div>';
  if(bb) bb.innerHTML=idle;
  if(ls) ls.innerHTML='';
  if(av) av.innerHTML='';
}
function capProofLabel(k){
  const b=CAP_BUCKETS.find(function(x){return x.k===k;}); return b?b.t:k;
}
/* 每行都带它自己的证据：status 是账本自报，verdict 是引擎 A/B 独立给的。两者分行摆，不合成一个。 */
function capRow(r){
  const tests=r.regression_tests||[];
  let h='<div class="caprow" data-p="'+esc(r.proof)+'">'
    +'<div class="cst" data-s="'+esc(r.status)+'">'+esc(r.status||'UNKNOWN')+'</div>'
    +'<div class="cmain"><div class="cid">'+esc(r.id||'<无 capability_id>')+'</div>'
    +'<div class="csub">'+esc(r.problem||r.mechanism||r.source||'（账上没写问题）')+'</div>'
    +'<div class="cpf" data-p="'+esc(r.proof)+'">'+esc(capProofLabel(r.proof));
  if(r.verdict) h+=' · <code>'+esc(r.verdict)+'</code>';
  if(r.ab_was!=null&&r.ab_now!=null) h+=' · A/B '+esc(r.ab_was)+'→'+esc(r.ab_now);
  if(r.degraded) h+=' · <span class="cvd" data-v="warn">降级运行</span>';
  h+='</div>';
  if(r.promotion_reason){
    h+='<details class="capdl"><summary>晋升理由（原话）</summary><p>'
      +esc(r.promotion_reason)+'</p></details>';
  }
  if(r.rejection_reason){
    h+='<details class="capdl"><summary>拒绝 / 回滚理由（原话）</summary><p>'
      +esc(r.rejection_reason)+'</p></details>';
  }
  if(tests.length){
    h+='<div class="cgids">关联测试 '+esc(tests.join(' · '))+'</div>';
  }
  h+='</div><div class="cmeta"><span><b>事件</b> '+esc(r.n_events)+'</span>'
    +'<span><b>历史</b> '+esc(r.history_n)+'</span>';
  if(r.autopilot) h+='<span><b>autopilot</b> <code>'+esc(r.autopilot)+'</code></span>';
  if(r.last_event) h+='<span><b>最近</b> '+esc(r.last_event)+'</span>';
  h+='</div></div>';
  return h;
}
function capBuckets(j){
  const n={'ab-verified':j.n_verified,'ab-unverifiable':j.n_unverifiable,
           'no-verdict':j.n_no_verdict,'anonymous':j.n_anonymous};
  let h='';
  CAP_BUCKETS.forEach(function(b){
    h+='<div class="capb" data-p="'+b.k+'"><h4>'+esc(b.t)+'</h4>'
      +'<div class="capbn">'+esc(n[b.k])+'</div>'
      +'<div class="capbd">'+esc(b.d)+'</div></div>';
  });
  /* 第五张：四桶合计必须等于账本总数。不齐就是判定逻辑漏了一条，
     这时候最该做的是把差额报出来，而不是把它塞进某一个桶里凑成一个整齐数字。 */
  const sum=j.n_verified+j.n_unverifiable+j.n_no_verdict+j.n_anonymous;
  const aligned=(sum===j.n_ledger);
  h+='<div class="capb capb-sum"><h4>四桶合计 / 账本总数</h4>'
    +'<div class="capbn">'+esc(sum)+' / '+esc(j.n_ledger)+'</div>'
    +'<div class="capbd">'
    +(aligned?'互斥且穷尽':'⚠ 不齐，差 '+esc(j.n_ledger-sum)+' 条：有账落不进任何一桶')
    +'</div></div>';
  return h;
}
function capAview(j){
  const vc=j.verdict_counts||{}, sc=j.status_counts||{};
  const vh=Object.keys(vc).sort().map(function(k){
    return '<span class="cvd" data-v="'+esc(k)+'"><code>'+esc(k)+'</code> '+esc(vc[k])+'</span>';
  }).join('');
  const sh=Object.keys(sc).sort().map(function(k){
    return '<span class="cst" data-s="'+esc(k)+'">'+esc(k)+' '+esc(sc[k])+'</span>';
  }).join('');
  const ap=(j.autopilot||[]).map(function(a){
    return '<li><code>'+esc(a.verdict||'?')+'</code> <b>'+esc(a.id||'?')+'</b>'
      +(a.reason?(' · '+esc(a.reason)):'')+'</li>';
  }).join('');
  /* 全量裁决记录分布 = regression_verdicts.jsonl 的**每一条记录**（含历史、
     含同一 id 被反复判过的多条），与上面的 vd_cnt（每条能力只取最新一条）是
     两件不同的事，两者都摆出来才不误导：只摆其中任一个，读者都会把「20 条
     HOLD-AB」当成裁决总量。 */
  const vc2=j.verdict_record_counts||{};
  const vh2=Object.keys(vc2).sort().map(function(k){
    return '<span class="cvd" data-v="'+esc(k)+'"><code>'+esc(k)+'</code> '+esc(vc2[k])+'</span>';
  }).join('');
  const vc1sum=Object.keys(vc).reduce(function(a,k){return a+vc[k];},0);
  const vc2sum=Object.keys(vc2).reduce(function(a,k){return a+vc2[k];},0);
  /* 悬空 id：裁决里有、能力账里没有。前端**不做判定**，只转发后端算好的
     n_verdict_dangling / dangling_ids —— 渲染层一旦自己「顺手」把这些 id 并进
     某个桶，就是在造一个后端从未声称过的结论。 */
  const dg=j.dangling_ids||[];
  const dgl=dg.map(function(x){
    return '<li><code>'+esc(x)+'</code></li>';
  }).join('');
  return '<div class="engc">'
    +'<h4>账本自报状态（status，**不等于**验过）</h4><div class="engrow">'+sh+'</div>'
    +'<h4>当前裁决分布（regression_verdicts，每条能力只取<strong>最新一条</strong>）</h4>'
    +'<div class="engrow">'+vh+'</div>'
    +'<p class="note">上面这块 <b>'+esc(vc1sum)+' 条</b>是「每条能力取 ts 最新一条」的'
    +'结果，<b>不是裁决总量</b>。同一条能力历史上被反复判过的记录、以及账本里'
    +'根本没有对应能力的裁决，都不在这里 —— 它们在下面两块。</p>'
    +'<h4>全量裁决记录分布（regression_verdicts 的每一条记录，含历史）</h4>'
    +'<div class="engrow">'+vh2+'</div>'
    +'<div class="engrow">'
    +'<span class="cvd" data-v="ok"><code>裁决记录总数</code> '+esc(j.n_verdict_records)+'</span>'
    +'<span class="cvd" data-v="'+(j.n_verdict_records<vc1sum?'fail':'ok')+'"><code>当前分布合计</code> '+esc(vc1sum)+'</span>'
    +'<span class="cvd" data-v="ok"><code>distinct 裁决 id</code> '+esc(j.n_verdict_ids)+'</span>'
    +'</div>'
    +'<h4>悬空裁决 id（裁决里有、能力账里没有）'
    +'<span class="gst '+(j.n_verdict_dangling?'warn':'ok')+'">'
    +esc(j.n_verdict_dangling)+'</span></h4>'
    +(dgl?('<ul class="capdl">'+dgl+'</ul>'):'<div class="idle">无悬空裁决 id</div>')
    +'<p class="note">这 '+esc(j.n_verdict_dangling)+' 个 id 在 regression_verdicts.jsonl 里'
    +'有独立 A/B 裁决记录，但 ledger.jsonl 里查不到对应能力行。它们<b>既没有被'
    +'并进上面的四个桶，也没有被删掉</b> —— 能力账上只有 36 行、其中 6 行连'
    +'capability_id 与 name 都没有。悬空 id 说明账与裁决已经脱节，需要有人去查'
    +'是能力行丢了、还是能力被改名/合并了；驾驶舱不替你猜，只把名单原样摆出来。</p>'
    +'<p class="note">「已验过」的定义：裁决落在 '
    +esc((j.verified_def||[]).join(' / '))
    +'。而这份裁决集里 <b>PASS=0</b> —— 引擎至今没有一次「这条能力真的变好了」的'
    +'独立裁决，所以一条都不进「已验过」桶。四桶的构成就是当前真实进度，'
    +'不需要再修饰。</p>'
    +'<h4>autopilot 裁决（逐条转发原话）</h4>'
    +(ap?('<ul class="capdl">'+ap+'</ul>'):'<div class="idle">无 autopilot 裁决</div>')
    +'<h4>A/B 快照与账本完整性</h4><div class="engrow">'
    +'<span class="cvd" data-v="ok"><code>ab_snapshots</code> '+esc(j.ab_snapshot_capabilities)+'</span>'
    +'<span class="cvd" data-v="'+(j.n_degraded?'warn':'ok')+'"><code>降级运行</code> '+esc(j.n_degraded)+'</span>'
    +'<span class="cvd" data-v="'+(j.bad_lines?'warn':'ok')+'"><code>坏行</code> '+esc(j.bad_lines)+'</span>'
    +'</div>'
    +'<div class="gmeta"><span>只读 · 账本一个字节都没写</span>'
    +'<span>'+esc(j.ledger_path||'—')+'</span></div></div>';
}
function capDraw(j){
  const bb=$('#capbuckets'), ls=$('#caplist'), av=$('#capav');
  if(!j||!j.ok){
    const m=(j&&j.error)||'/__capmap 不可达';
    if(ls) ls.innerHTML='';
    if(av) av.innerHTML='';
    if(bb) bb.innerHTML='<div class="gcard gblock"><h4>能力账本读不出来'
      +'<span class="gst block">BLOCK</span></h4>'
      +'<p class="ggoal">'+esc(m)+'</p>'
      +'<div class="gmeta"><span>读不到账本就不给「没有能力 / 一切正常」这种结论 —— '
      +'拿不到判据不给就绪度（fail-closed）。台面不替你猜。</span></div></div>';
    caphint('BLOCK：'+m,'no');
    return;
  }
  const qel=$('#capq'), fel=$('#capf');
  const q=((qel&&qel.value)||'').trim().toLowerCase();
  const f=((fel&&fel.value)||'');
  /* 搜索与桶筛选只动**展示层**，四桶卡片上的计数恒为全量账的计数。
     如果筛选能把「已验过 20」改成 6，用户会以为账本里只剩 6 条验过的。 */
  const rows=(j.rows||[]).filter(function(r){
    if(f&&r.proof!==f) return false;
    if(!q) return true;
    const hay=[r.id,r.problem,r.source,r.mechanism,r.implementation,
               r.promotion_reason,r.rejection_reason].concat(r.regression_tests||[]);
    return hay.filter(Boolean).join(' ').toLowerCase().indexOf(q)>=0;
  });
  if(bb) bb.innerHTML=capBuckets(j);
  if(ls){
    if(!rows.length){
      ls.innerHTML='<div class="gcard"><h4>0 条命中</h4><div class="gmeta"><span>'
        +'<b>这是筛选结果为 0，不是账本为 0</b>。账本全量 '+esc(j.total)
        +' 条，上面四桶的计数才是账本计数。</span></div></div>';
    }else{
      ls.innerHTML='<p class="note">显示 '+esc(rows.length)+' / 全量 '+esc(j.total)
        +' 条（本次读入上限 '+esc((j.rows||[]).length)+'）</p>'
        +'<div class="caprows">'+rows.map(capRow).join('')+'</div>';
    }
  }
  if(av) av.innerHTML=capAview(j);
  caphint('已读 · 账本 '+j.n_ledger+' 条 · 已验过 '+j.n_verified
    +' / 验不了 '+j.n_unverifiable+' / 没跑过 '+j.n_no_verdict
    +' / 无身份 '+j.n_anonymous,'ok');
}
function capRedraw(){ if(CAPLAST) capDraw(CAPLAST); }
async function capLoad(){
  if(CAPBUSY){ caphint('上一次读账本还在跑 —— 等它回来','no'); return; }
  CAPBUSY=true; const b=$('#capgo'); if(b) b.disabled=true;
  caphint('读能力账本…（只读 ledger.jsonl × regression_verdicts.jsonl，不写盘）');
  let j;
  try{
    const r=await fetch('/__capmap');
    if(!r.ok) throw new Error('HTTP '+r.status);
    j=await r.json();
  }catch(e){ j={ok:false,error:'NETWORK '+e.message}; }
  CAPBUSY=false; if(b) b.disabled=false;
  CAPLAST=j; capDraw(j);
}
boot();

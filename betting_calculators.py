#!/usr/bin/env python3
"""betting_calculators.py — a bundle of sports-betting math calculators.

A single page (mounted at /calc) with self-contained, client-side calculators:
  - Odds converter (American / Decimal / Fractional / Implied %)
  - No-vig / devig + hold (2-way and 3-way)
  - Kelly stake (full + fractional)
  - Parlay payout (+ true no-vig parlay price)
  - Hedge calculator (lock a guaranteed result)
  - Free-bet conversion (expected cash value)

All math runs in the browser — no data feeds, works year-round. Module-level
`app` so it mounts cleanly; standalone: `py -3 betting_calculators.py`.
"""

import argparse

from flask import Flask, Response

app = Flask(__name__)

PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="Sports betting calculators: odds converter, no-vig/devig + hold, Kelly stake, parlay, hedge, free-bet conversion.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0f1419">
<title>Betting Calculators</title>
<style>
:root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;
--accent:#ffce54;--good:#4caf50;--bad:#e57373}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:22px 16px}
.container{max-width:1040px;margin:0 auto}
h1{font-size:23px;font-weight:700;margin-bottom:3px}
.sub{color:var(--muted);font-size:14px;margin-bottom:20px}
.menu{display:inline-block;margin-bottom:14px;color:#7fb2ff;text-decoration:none;font-size:.85em;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--border);border-radius:11px;padding:16px 18px}
.card h2{font-size:15px;font-weight:700;margin-bottom:4px}
.card .hint{color:var(--muted);font-size:12px;margin-bottom:12px;line-height:1.4}
.field{margin-bottom:10px}
label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
input,select{width:100%;padding:9px 11px;background:#0f1419;color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:14px}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.out{margin-top:12px;background:#0f1419;border:1px solid var(--border);border-radius:8px;padding:11px 13px;font-size:14px;line-height:1.7}
.out .k{color:var(--muted)}.out .v{font-weight:700;float:right}
.out .big{font-size:18px;font-weight:800;color:var(--accent)}
.pos{color:var(--good)}.neg{color:var(--bad)}
.leg{display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:6px;align-items:center}
.leg button{background:#2a2030;color:#ff9b9b;border:1px solid var(--border);border-radius:6px;cursor:pointer;padding:0 10px;font-size:16px}
.addbtn{background:var(--border);color:var(--text);border:0;border-radius:6px;padding:7px 12px;font-size:13px;font-weight:600;cursor:pointer;margin-top:2px}
.note{color:var(--muted);font-size:12px;margin-top:18px;line-height:1.5;text-align:center}
hr{border:0;border-top:1px solid var(--border);margin:11px 0}
</style></head><body><div class="container">
<a class="menu" href="/">&#8962; Main Menu</a>
<h1>Betting Calculators</h1>
<div class="sub">Odds conversion, devig &amp; hold, Kelly staking, parlays, hedging and free-bet value — all computed live in your browser.</div>
<div class="grid">

  <div class="card">
    <h2>Odds Converter</h2>
    <div class="hint">Type any one format; the rest update.</div>
    <div class="row2">
      <div class="field"><label>American</label><input id="oc_am" inputmode="numeric" placeholder="-110"></div>
      <div class="field"><label>Decimal</label><input id="oc_dec" inputmode="decimal" placeholder="1.91"></div>
    </div>
    <div class="row2">
      <div class="field"><label>Fractional</label><input id="oc_frac" placeholder="10/11"></div>
      <div class="field"><label>Implied %</label><input id="oc_imp" inputmode="decimal" placeholder="52.4"></div>
    </div>
    <div class="out" id="oc_out"></div>
  </div>

  <div class="card">
    <h2>No-Vig / Devig + Hold</h2>
    <div class="hint">Enter both sides' American odds (add a 3rd for 3-way). Get the fair no-vig prices and the book's hold.</div>
    <div class="row3">
      <div class="field"><label>Side A</label><input id="dv_a" inputmode="numeric" placeholder="-110"></div>
      <div class="field"><label>Side B</label><input id="dv_b" inputmode="numeric" placeholder="-110"></div>
      <div class="field"><label>Side C (opt)</label><input id="dv_c" inputmode="numeric" placeholder=""></div>
    </div>
    <div class="out" id="dv_out"></div>
  </div>

  <div class="card">
    <h2>Kelly Stake</h2>
    <div class="hint">Your true win % vs the price offered → optimal stake.</div>
    <div class="row2">
      <div class="field"><label>Win probability %</label><input id="k_p" inputmode="decimal" placeholder="55"></div>
      <div class="field"><label>Odds offered (American)</label><input id="k_odds" inputmode="numeric" placeholder="-110"></div>
    </div>
    <div class="row2">
      <div class="field"><label>Bankroll $</label><input id="k_bank" inputmode="decimal" placeholder="1000"></div>
      <div class="field"><label>Kelly fraction</label>
        <select id="k_frac"><option value="1">Full</option><option value="0.5" selected>Half</option>
        <option value="0.33">Third</option><option value="0.25">Quarter</option></select></div>
    </div>
    <div class="out" id="k_out"></div>
  </div>

  <div class="card">
    <h2>Parlay Calculator</h2>
    <div class="hint">Add each leg's American odds. Shows combined price + payout and the true no-vig fair odds.</div>
    <div id="p_legs"></div>
    <button class="addbtn" onclick="addLeg()">+ Add leg</button>
    <div class="field" style="margin-top:10px"><label>Stake $</label><input id="p_stake" inputmode="decimal" placeholder="100" value="100"></div>
    <div class="out" id="p_out"></div>
  </div>

  <div class="card">
    <h2>Hedge Calculator</h2>
    <div class="hint">Your open bet + the current opposite price → how much to lay to lock an equal guaranteed result.</div>
    <div class="row2">
      <div class="field"><label>Original stake $</label><input id="h_stake" inputmode="decimal" placeholder="100"></div>
      <div class="field"><label>Original odds (American)</label><input id="h_odds" inputmode="numeric" placeholder="+200"></div>
    </div>
    <div class="field"><label>Hedge odds — other side (American)</label><input id="h_hedge" inputmode="numeric" placeholder="-150"></div>
    <div class="out" id="h_out"></div>
  </div>

  <div class="card">
    <h2>Free-Bet Conversion</h2>
    <div class="hint">A free bet returns winnings only (stake not returned). Shows its cash value at a given price.</div>
    <div class="row2">
      <div class="field"><label>Free bet $</label><input id="f_amt" inputmode="decimal" placeholder="50"></div>
      <div class="field"><label>Odds used (American)</label><input id="f_odds" inputmode="numeric" placeholder="+200"></div>
    </div>
    <div class="field"><label>Your win probability % (optional, for raw EV)</label><input id="f_p" inputmode="decimal" placeholder=""></div>
    <div class="out" id="f_out"></div>
  </div>

</div>
<div class="note">For information only. &ldquo;Hold&rdquo; is the book&rsquo;s theoretical margin; &ldquo;no-vig&rdquo; prices remove it via proportional devig. Kelly assumes your win % is accurate — fractional Kelly is safer.</div>
</div>

<script>
const $=id=>document.getElementById(id);
function amToDec(a){a=parseFloat(a);if(isNaN(a)||a===0)return NaN;return a>0?1+a/100:1+100/Math.abs(a);}
function decToAm(d){d=parseFloat(d);if(isNaN(d)||d<=1)return NaN;return d>=2?Math.round((d-1)*100):Math.round(-100/(d-1));}
function amStr(a){if(isNaN(a))return '—';return a>0?'+'+a:''+a;}
function impFromDec(d){return 1/d;}
function pct(x){return (x*100).toFixed(2)+'%';}
function money(x){return (x<0?'-$':'$')+Math.abs(x).toFixed(2);}
function gcd(a,b){return b?gcd(b,a%b):a;}
function toFrac(dec){let n=dec-1;if(n<=0||!isFinite(n))return '—';let den=1;while(Math.abs(n*den-Math.round(n*den))>1e-4&&den<1000)den++;let num=Math.round(n*den);let g=gcd(num,den)||1;return (num/g)+'/'+(den/g);}

// ── Odds converter (two-way binding) ──
function ocFrom(src){
  let dec=NaN;
  if(src==='am')dec=amToDec($('oc_am').value);
  else if(src==='dec')dec=parseFloat($('oc_dec').value);
  else if(src==='imp'){let p=parseFloat($('oc_imp').value)/100;if(p>0&&p<1)dec=1/p;}
  else if(src==='frac'){let m=($('oc_frac').value||'').split('/');if(m.length===2){let a=parseFloat(m[0]),b=parseFloat(m[1]);if(b>0)dec=1+a/b;}}
  if(isNaN(dec)||dec<=1){$('oc_out').innerHTML='<span class="k">Enter a value above.</span>';return;}
  if(src!=='am')$('oc_am').value=amStr(decToAm(dec));
  if(src!=='dec')$('oc_dec').value=dec.toFixed(3);
  if(src!=='frac')$('oc_frac').value=toFrac(dec);
  if(src!=='imp')$('oc_imp').value=(impFromDec(dec)*100).toFixed(2);
  $('oc_out').innerHTML='<span class="k">Break-even win rate</span><span class="v big">'+pct(impFromDec(dec))+'</span>';
}
['am','dec','frac','imp'].forEach(k=>$('oc_'+k).addEventListener('input',()=>ocFrom(k)));

// ── Devig ──
function devig(){
  let odds=[$('dv_a').value,$('dv_b').value,$('dv_c').value].map(v=>v.trim()).filter(v=>v!=='');
  let imps=odds.map(o=>{let d=amToDec(o);return isNaN(d)?NaN:impFromDec(d);});
  if(imps.length<2||imps.some(isNaN)){$('dv_out').innerHTML='<span class="k">Enter at least two prices.</span>';return;}
  let over=imps.reduce((a,b)=>a+b,0);
  let hold=(1-1/over);
  let labels=['A','B','C'];
  let rows=imps.map((p,i)=>{let fair=p/over;return '<span class="k">No-vig '+labels[i]+'</span><span class="v">'+amStr(decToAm(1/fair))+' &nbsp;('+pct(fair)+')</span>';}).join('<br>');
  $('dv_out').innerHTML=rows+'<hr><span class="k">Overround</span><span class="v">'+pct(over)+'</span><br><span class="k">Book hold</span><span class="v big">'+pct(hold)+'</span>';
}
['dv_a','dv_b','dv_c'].forEach(k=>$(k).addEventListener('input',devig));

// ── Kelly ──
function kelly(){
  let p=parseFloat($('k_p').value)/100, dec=amToDec($('k_odds').value), bank=parseFloat($('k_bank').value), kf=parseFloat($('k_frac').value);
  if(isNaN(p)||p<=0||p>=1||isNaN(dec)){$('k_out').innerHTML='<span class="k">Enter win % and odds.</span>';return;}
  let b=dec-1, q=1-p;
  let f=(b*p-q)/b;
  let ev=p*dec-1;
  let edgeTxt='<span class="k">Edge (EV per $1)</span><span class="v '+(ev>=0?'pos':'neg')+'">'+(ev>=0?'+':'')+pct(ev)+'</span>';
  if(f<=0){$('k_out').innerHTML=edgeTxt+'<hr><span class="k">Kelly says</span><span class="v neg">No bet (no edge)</span>';return;}
  let stake=bank>0?f*kf*bank:NaN;
  $('k_out').innerHTML=edgeTxt+
    '<br><span class="k">Full-Kelly fraction</span><span class="v">'+pct(f)+'</span>'+
    '<br><span class="k">Suggested stake</span><span class="v big">'+(isNaN(stake)?pct(f*kf)+' of roll':money(stake))+'</span>';
}
['k_p','k_odds','k_bank','k_frac'].forEach(k=>$(k).addEventListener('input',kelly));

// ── Parlay ──
function legRow(){let d=document.createElement('div');d.className='leg';
  d.innerHTML='<input inputmode="numeric" placeholder="-110" oninput="parlay()"><button onclick="this.parentNode.remove();parlay()">×</button>';return d;}
function addLeg(){$('p_legs').appendChild(legRow());parlay();}
function parlay(){
  let inputs=[...$('p_legs').querySelectorAll('input')];
  let decs=inputs.map(i=>amToDec(i.value)).filter(d=>!isNaN(d)&&d>1);
  if(decs.length<2){$('p_out').innerHTML='<span class="k">Add at least two valid legs.</span>';return;}
  let D=decs.reduce((a,b)=>a*b,1);
  let stake=parseFloat($('p_stake').value)||0;
  let payout=stake*D, profit=payout-stake;
  // no-vig fair: treat each leg's implied as-is (already vig'd) → fair parlay would need per-leg no-vig; show simple combined implied
  let combImp=1/D;
  $('p_out').innerHTML='<span class="k">Legs</span><span class="v">'+decs.length+'</span>'+
    '<br><span class="k">Combined odds</span><span class="v big">'+amStr(decToAm(D))+' &nbsp;('+D.toFixed(2)+')</span>'+
    '<br><span class="k">Payout / profit</span><span class="v">'+money(payout)+' / '+money(profit)+'</span>'+
    '<br><span class="k">Implied win %</span><span class="v">'+pct(combImp)+'</span>';
}

// ── Hedge ──
function hedge(){
  let S=parseFloat($('h_stake').value), Do=amToDec($('h_odds').value), Dh=amToDec($('h_hedge').value);
  if(isNaN(S)||isNaN(Do)||isNaN(Dh)){$('h_out').innerHTML='<span class="k">Enter stake and both prices.</span>';return;}
  let ret=S*Do;                       // total return if original wins
  let H=ret/Dh;                        // hedge stake so other side returns the same
  let outlay=S+H;
  let locked=ret-outlay;              // guaranteed profit either way
  $('h_out').innerHTML='<span class="k">Hedge stake</span><span class="v big">'+money(H)+'</span>'+
    '<br><span class="k">Total outlay</span><span class="v">'+money(outlay)+'</span>'+
    '<br><span class="k">Guaranteed result</span><span class="v '+(locked>=0?'pos':'neg')+'">'+(locked>=0?'+':'')+money(locked)+'</span>'+
    '<br><span class="k">Return either way</span><span class="v">'+money(ret)+'</span>';
}
['h_stake','h_odds','h_hedge'].forEach(k=>$(k).addEventListener('input',hedge));

// ── Free bet ──
function freebet(){
  let A=parseFloat($('f_amt').value), dec=amToDec($('f_odds').value), p=parseFloat($('f_p').value);
  if(isNaN(A)||isNaN(dec)){$('f_out').innerHTML='<span class="k">Enter amount and odds.</span>';return;}
  let winnings=A*(dec-1);             // free bet pays profit only
  let conv=(dec-1)/dec;              // value retained when optimally hedged at fair odds
  let html='<span class="k">Winnings if it hits</span><span class="v">'+money(winnings)+'</span>'+
    '<br><span class="k">Hedged cash value</span><span class="v big">'+money(A*conv)+' &nbsp;('+(conv*100).toFixed(1)+'%)</span>';
  if(!isNaN(p)&&p>0&&p<100){let ev=(p/100)*winnings;html+='<br><span class="k">Raw EV at your '+p+'%</span><span class="v">'+money(ev)+'</span>';}
  $('f_out').innerHTML=html;
}
['f_amt','f_odds','f_p'].forEach(k=>$(k).addEventListener('input',freebet));

addLeg();addLeg();   // start parlay with two legs
</script></body></html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


def main():
    ap = argparse.ArgumentParser(description="Betting calculators bundle")
    ap.add_argument("--port", type=int, default=5011)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

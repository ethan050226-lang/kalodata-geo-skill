const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(require('path').join(__dirname,'skill/geo-sampling/assets/dashboard.html'),'utf8');
const fn=html.slice(html.indexOf('function renderScenarios('),html.indexOf('function render(){'));
let output;const context={D:{groups:{g:'对比'},tags:{h:'品牌健康',d:'发现度'}},$:()=>({set innerHTML(v){output=v}}),total:rs=>rs.reduce((s,r)=>s+r.n,0),sum:(rs,f)=>rs.reduce((s,r)=>s+f(r),0),num:String,ratio:(a,b)=>`${a}/${b}`,mentionCount:r=>r.mentions.Kalodata-(r.negative_mentions||0),table:(h,rows)=>rows};
vm.createContext(context);vm.runInContext(fn,context);
const row=(named,tags)=>({d:['US','chatgpt','2026-11-09','g',named,tags],n:2,mentions:{Kalodata:2},negative_mentions:1,first:1,rank:[3,2],sent:{positive:1,negative:1},links:{Kalodata:[1,2,1,2]}});
context.renderScenarios([row(0,['h','d']),row(1,[])]);
assert(output[0][2].includes('1/2'));assert(output[1][1]==='2');assert(output[2][2].includes('1.50'));assert(output[3][2].includes('负面 1/2'));assert(output[3][3].includes('2/2'));
context.renderScenarios([]);assert(output.every(r=>r[2]==='暂无数据'));
console.log('Scene calculations passed');
const scripts=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>!x.includes('__GEO_PAYLOAD__'));for(const s of scripts)new vm.Script(s);console.log('JavaScript syntax passed');
// Hand-calculated mixed sample: unnamed hit 1/2 after one negative; first 1/2; rank 3/2.
vm.runInContext(html.slice(html.indexOf('function compareStats('),html.indexOf('function renderComparison(')),context);
const sample={...row(0,['d']),rec:{explicit:1,unknown:1}};const stats=context.compareStats([sample]);
assert.deepEqual(Array.from(stats.unmentioned),[1,2]);assert.deepEqual(Array.from(stats.first),[1,2]);assert.deepEqual(Array.from(stats.rank),[3,2]);
const emptyStats=context.compareStats([]);assert.deepEqual(Array.from(emptyStats.first),[0,0]);assert.deepEqual(Array.from(emptyStats.rank),[0,0]);
console.log('Metric numerator/denominator checks passed');

// Matrix cells use only unnamed answers; duplicate tag membership never adds within a cell.
context.D.markets={US:'美国',JP:'日本'};context.D.tags={d:'发现度',h:'健康'};

const cellRow={...row(0,['d','h']),rec:{},sent:{positive:1,unknown:1}};
const namedRow={...cellRow,d:['US','chatgpt','2026-11-09','g',1,['d']]};
const matrix=context.matrixData([cellRow,namedRow],'group');assert.equal(matrix.cells[0][0].n,2);assert.equal(matrix.cells[0][0].hits,1);
assert.equal(context.matrixData([namedRow],'group').columns.length,0);
const tags=context.matrixData([cellRow],'tag');assert.equal(tags.cells[0][0].n,2);assert.equal(tags.cells[0][1].n,2);
const sent=context.sentimentCounts([cellRow]);assert.equal(sent.known,1);assert.equal(sent.counts.reduce((a,b)=>a+b,0),2);
assert.equal(context.sentimentCounts([]).n,0);
console.log('Matrix, unknown reputation and zero-sample checks passed');
context.batch={begin_date:'2026-11-01',end_date:'2026-11-07'};
const allFilters={m:[],e:[],g:[],tags:[],named:[],from:context.batch.begin_date,to:context.batch.end_date};
context.D.comparison={previous_batch:{},limitations:[],metrics:[{id:'skill.first_rate',change:25}]};
assert(context.cardHistory('skill.first_rate',allFilters).includes('+25.00'));
assert(context.cardHistory('skill.first_rate',{...allFilters,m:['US']}).includes('暂不比较'));
context.D.comparison.limitations=['题库版本不同'];assert(context.cardHistory('skill.first_rate',allFilters).includes('上次不可比'));
console.log('Card history scope checks passed');

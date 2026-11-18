const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('assert'),fs=require('fs');
const fileUrl=require('url').pathToFileURL(require('path').resolve(process.argv[2])).href;
(async()=>{const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.goto(fileUrl);
const picker=side=>page.locator('#filter-'+side+' [data-key="m"]');
const selected=side=>picker(side).locator('input:checked').evaluateAll(items=>items.map(i=>i.value).sort());
async function checkInitialView(){
  const config=await page.evaluate(()=>({
    defaults:D.market_scope?.default_markets||[],
    markets:Object.keys(D.markets).sort(),
    rate:pct(sum(D.rows.filter(r=>matches(r.d,filters())&&!r.d[4]),r=>mentionCount(r)),
      total(D.rows.filter(r=>matches(r.d,filters())&&!r.d[4])))
  }));
  for(const side of ['a','b','review']){
    assert.deepEqual(await selected(side),config.defaults);
    assert.deepEqual(await picker(side).locator('input').evaluateAll(items=>items.map(i=>i.value).sort()),config.markets);
  }
  assert.equal(await page.locator('#kpis .value').first().innerText(),config.rate);
  return config;
}
const initial=await checkInitialView();
await page.goto(fileUrl+'#brand');
assert.deepEqual(await page.locator('#brand-market option').evaluateAll(items=>items.map(i=>i.value).filter(Boolean).sort()),initial.markets);
const otherCountry=await page.evaluate(()=>D.rows.find(r=>!(D.market_scope.default_markets||[]).includes(r.d[0]))?.d[0]);
if(otherCountry){
  await page.locator('#brand-market').selectOption(otherCountry);
  assert.deepEqual(await selected('a'),[otherCountry]);
  assert.deepEqual(await selected('review'),[otherCountry]);
  assert((await page.locator('#mention-title').textContent()).includes(await page.evaluate(m=>D.markets[m],otherCountry)));
}
await page.goto(fileUrl);
// An approved default country with no answers keeps other countries selectable.
if(process.argv[3]){
  const emptyUrl=require('url').pathToFileURL(require('path').resolve(process.argv[3])).href;
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    await page.goto(emptyUrl);
    const config=await checkInitialView();
    assert((await page.locator('#scope').innerText()).includes('暂无数据'));
    assert.equal(await page.locator('#kpis .value').first().innerText(),'不适用');
    assert.equal(await page.locator('#home-matrix .matrix-cell, #home-actions .action-item').count(),0);
    const markets=await page.evaluate(()=>[...new Set(D.rows.map(r=>r.d[0]))]);
    assert(markets.length>=2);
    const check=async(side,market)=>picker(side).locator('input[value="'+market+'"]').check();
    await picker('a').locator('summary').click();
    await picker('a').locator('button').click();
    assert.deepEqual(await selected('a'),[]);
    assert.deepEqual(await selected('review'),[]);
    await check('a',markets[0]);await check('a',markets[1]);
    assert.deepEqual(await selected('review'),markets.slice(0,2).sort());
    await picker('a').locator('input[value="'+markets[1]+'"]').uncheck();
    assert.equal(await page.locator('#kpis .value').first().innerText(),await page.evaluate(m=>{
      const rows=D.rows.filter(r=>r.d[0]===m&&!r.d[4]);return pct(sum(rows,r=>mentionCount(r)),total(rows));
    },markets[0]));
    for(const name of ['brand','action','answers','sources']){
      await page.goto(emptyUrl+'#'+name);
      assert.deepEqual(await selected('a'),[markets[0]]);
      assert.deepEqual(await selected('review'),[markets[0]]);
      assert.equal(await page.evaluate(()=>D.rows.filter(r=>matches(r.d,filters())).some(r=>r.d[0]!==filters().m[0])),false);
    }
    await page.goto(emptyUrl+'#compare');
    await picker('b').locator('summary').click();
    await picker('b').locator('button').click();await check('b',markets[1]);
    assert.deepEqual(await selected('a'),[markets[0]]);
    assert((await page.locator('.cohort.b').innerText()).includes(await page.evaluate(m=>D.markets[m],markets[1])));
    await page.locator('#filter-a').getByText('重置',{exact:true}).click();
    assert.deepEqual(await selected('a'),config.defaults);
    assert.deepEqual(await selected('review'),config.defaults);
  }
}
// No preference opens the complete batch and reset returns to all countries.
if(process.argv[4]){
  const allUrl=require('url').pathToFileURL(require('path').resolve(process.argv[4])).href;
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    await page.goto(allUrl);
    const config=await checkInitialView();assert.deepEqual(config.defaults,[]);
    assert.equal(await picker('a').locator('summary').textContent(),'全部国家');
    await picker('a').locator('summary').click();
    await picker('a').locator('input').first().check();
    assert.equal((await selected('a')).length,1);
    await page.locator('#filter-a').getByText('重置',{exact:true}).click();
    assert.deepEqual(await selected('a'),[]);
    assert.equal(await page.locator('#kpis .value').first().innerText(),config.rate);
  }
}
await page.goto(fileUrl);
for(const width of [1440,390,320]){await page.setViewportSize({width,height:1000});await page.goto(fileUrl+'#overview');await page.waitForTimeout(100);assert.equal(await page.locator('.page-tabs a').count(),5);assert.equal(await page.locator('#kpis .kpi').count(),3);assert(await page.locator('#home-matrix .matrix-cell').count()>0);assert(await page.locator('#home-sentiment .sent-stack').count()>0);const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert(!overflow,'page overflow at '+width);if(process.env.SCREENSHOT_DIR)await page.screenshot({path:require('path').join(process.env.SCREENSHOT_DIR,'geo-'+width+'.png'),fullPage:true});}
for(const name of ['brand','platform','compare','action','answers','sources','metrics']){await page.goto(fileUrl+'#'+name);assert(await page.locator('#'+name).isVisible());assert.equal(await page.locator('[data-page]:visible').count(),1);if(name==='sources')assert(await page.locator('#sentiment').isVisible());}
await page.goto(fileUrl+'#overview');await page.waitForTimeout(50);
const expected=await page.evaluate(()=>{const m=D.rows[0].d[0];setFilters('a',{...filters(),m:[m]});render();const rows=D.rows.filter(r=>r.d[0]===m&&!r.d[4]);const den=rows.reduce((a,r)=>a+r.n,0),hits=rows.reduce((a,r)=>a+mentionCount(r),0);return pct(hits,den);});
assert.equal(await page.locator('#kpis .value').first().textContent(),expected);assert(await page.locator('#home-matrix .matrix-cell').count()>0);
await page.locator('#matrix-dimension').selectOption('tag');assert(await page.locator('#home-matrix .matrix-cell').count()>0);
await page.locator('#matrix-dimension').selectOption('group');const cell=page.locator('#home-matrix .matrix-cell').first();const expectedCell=await cell.locator('b').textContent();await cell.click();assert.equal(await page.locator('#kpis .value').first().textContent(),expectedCell);
await page.goto(fileUrl+'#action');assert(await page.locator('#next .action-item').count()>0);
await page.goto(fileUrl+'#overview');assert.equal(await page.locator('#overview #named-results').count(),0);assert.equal(await page.locator('#data-reminder').getAttribute('open'),null);
await page.locator('#data-reminder summary').click();assert(await page.locator('#status-details').isVisible());assert((await page.locator('#home-status').textContent()).includes('数据提醒'));
await page.goto(fileUrl+'#brand');await page.evaluate(()=>{setFilters('a',{m:[],e:[],g:[],tags:[],named:['1'],tagMode:'all',from:batch.begin_date,to:batch.end_date});render();});await page.locator('.named-topic summary').click();assert(await page.locator('#named-results').isVisible());assert((await page.locator('#named-results').textContent()).includes('初判'));
await page.evaluate(()=>{setFilters('a',{...filters(),named:['0']});render();});assert((await page.locator('#named-results').textContent()).includes('没有点名题'));
assert.deepEqual(errors,[]);console.log('Desktop/mobile/navigation checks passed');await browser.close();})().catch(e=>{console.error(e);process.exit(1)});

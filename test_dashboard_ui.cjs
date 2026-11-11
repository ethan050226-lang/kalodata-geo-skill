const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('assert'),fs=require('fs');
const fileUrl=require('url').pathToFileURL(require('path').resolve(process.argv[2])).href;
(async()=>{const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
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

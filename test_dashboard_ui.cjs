const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('assert'),fs=require('fs');
const fileUrl=require('url').pathToFileURL(require('path').resolve(process.argv[2])).href;
(async()=>{const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.goto(fileUrl);
for(const width of [1440,390,320]){await page.setViewportSize({width,height:1000});await page.goto(fileUrl+'#overview');await page.waitForTimeout(100);assert.equal(await page.locator('.page-tabs a').count(),5);assert.equal(await page.locator('#kpis .kpi').count(),4);assert(await page.locator('#home-market-chart .bar').count()>0);const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert(!overflow,'page overflow at '+width);if(process.env.SCREENSHOT_DIR)await page.screenshot({path:require('path').join(process.env.SCREENSHOT_DIR,'geo-'+width+'.png'),fullPage:true});}
for(const name of ['brand','platform','compare','action','answers','sources','metrics']){await page.goto(fileUrl+'#'+name);assert(await page.locator('#'+name).isVisible());assert.equal(await page.locator('[data-page]:visible').count(),1);if(name==='sources')assert(await page.locator('#sentiment').isVisible());}
await page.goto(fileUrl+'#overview');await page.waitForTimeout(50);
const expected=await page.evaluate(()=>{const m=D.rows[0].d[0];setFilters('a',{...filters(),m:[m]});render();const rows=D.rows.filter(r=>r.d[0]===m&&!r.d[4]);const den=rows.reduce((a,r)=>a+r.n,0),hits=rows.reduce((a,r)=>a+mentionCount(r),0);return pct(hits,den);});
assert.equal(await page.locator('#kpis .value').first().textContent(),expected);assert.equal(await page.locator('#home-market-chart .bar').count(),1);
assert.deepEqual(errors,[]);console.log('Desktop/mobile/navigation checks passed');await browser.close();})().catch(e=>{console.error(e);process.exit(1)});

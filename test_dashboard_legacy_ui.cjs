const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('assert');
const fs=require('fs');
const path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8');
const payload=JSON.parse(html.match(/<script id="geo-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const defaults=payload.presentation.default_markets;
const sum=(rows,fn)=>rows.reduce((n,r)=>n+fn(r),0);
const rate=markets=>{
  const rows=payload.rows.filter(r=>!r.d[4]&&(!markets.length||markets.includes(r.d[0])));
  return (100*sum(rows,r=>(r.mentions.Kalodata||0)-(r.negative_mentions||0))/sum(rows,r=>r.n)).toFixed(2)+'%';
};
(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  try{
    const context=await browser.newContext({offline:true});
    await context.route('**/*',route=>route.abort());
    let page;
    const errors=[];
    const picker=side=>page.locator('#filter-'+side+' [data-key="m"]');
    const selected=side=>picker(side).locator('input:checked').evaluateAll(items=>items.map(i=>i.value).filter(v=>v!=='__all').sort());
    const outcome={viewports:[],actions:0,question_ids:0};
    for(const width of [1440,390]){
      if(page)await page.close();
      page=await context.newPage();
      page.on('pageerror',error=>errors.push(error.message));
      await page.setViewportSize({width,height:1000});
      // The final HTML is rendered entirely offline, without navigating to local or external URLs.
      await page.setContent(html);
      for(const side of ['a','b','review']){
        assert.deepEqual(await selected(side),defaults);
        assert.equal(await picker(side).locator('input:not([value="__all"])').count(),Object.keys(payload.markets).length);
      }
      assert.equal(await page.locator('#kpis .value').first().innerText(),rate(defaults));
      assert((await page.locator('#scope').innerText()).includes('历史回答'));
      assert((await page.locator('#headline').innerText()).includes('提及率'));
      const reminder=await page.locator('#home-status').innerText();
      assert(reminder.includes('另'));
      assert(reminder.length<140);
      if(process.env.SCREENSHOT_DIR){
        fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'overview-'+width+'.png')});
      }
      await page.locator('.page-tabs a[href="#action"]').click();
      const count=await page.locator('#next .action-item').count();
      assert(count>0);
      outcome.actions=count;
      const ids=await page.locator('#next .question>b').allTextContents();
      const references=payload.rows.filter(r=>defaults.includes(r.d[0])).flatMap(r=>Object.values(r.evidence||{}));
      references.push(...(payload.review?.rows||[]).filter(ref=>defaults.includes(ref.d[0])));
      const evidence=new Set(references.map(ref=>ref.q));
      assert(ids.every(id=>evidence.has(id)),JSON.stringify(ids.filter(id=>!evidence.has(id)).slice(0,5)));
      outcome.question_ids=new Set(ids).size;
      const actionScopes=await page.locator('#next .action-scope').allTextContents();
      assert(actionScopes.every(scope=>defaults.some(m=>scope.includes(payload.markets[m]))));
      assert((await page.locator('#action-count').innerText()).includes('核查线索'));
      if(process.env.SCREENSHOT_DIR)await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'actions-'+width+'.png')});
      await page.locator('.page-tabs a[href="#overview"]').click();
      await picker('a').locator('summary').click();
      const all=picker('a').locator('input[value="__all"]');
      await all.check();
      assert.equal(await picker('a').locator('input:not([value="__all"])').evaluateAll(items=>items.every(i=>i.disabled&&!i.checked)),true);
      assert.equal(await page.locator('#kpis .value').first().innerText(),rate([]));
      await all.uncheck();
      assert.equal(await page.locator('#kpis .value').first().innerText(),'不适用');
      await picker('a').locator('input[value="JP"]').check();
      assert.equal(await page.locator('#kpis .value').first().innerText(),rate(['JP']));
      await page.locator('#filter-a').getByText('重置',{exact:true}).click();
      assert.deepEqual(await selected('a'),defaults);
      assert.equal(await page.locator('#kpis .value').first().innerText(),rate(defaults));
      await page.locator('.page-tabs a[href="#brand"]').click();
      await page.locator('#workspace-nav a[href="#compare"]').click();
      await picker('b').locator('summary').click();
      for(const market of defaults)await picker('b').locator('input[value="'+market+'"]').uncheck();
      await picker('b').locator('input[value="JP"]').check();
      assert.deepEqual(await selected('a'),defaults);
      assert.deepEqual(await selected('b'),['JP']);
      assert((await page.locator('.cohort.b').innerText()).includes('日本'));
      await page.locator('.page-tabs a[href="#metrics"]').click();
      assert(await page.locator('#metric-core').isVisible());
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.deepEqual(errors,[]);
      outcome.viewports.push(width);
    }
    if(process.argv[3]){
      const report=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
      const mention=report.metrics.find(m=>m.id==='skill.core_mention_rate');
      assert.equal(mention.value.toFixed(2)+'%',rate(defaults));
    }
    console.log(JSON.stringify(outcome));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});

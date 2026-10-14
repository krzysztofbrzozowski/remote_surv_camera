/* Run against the isolated demo server, never against a production login.
 * Requires Playwright, its WebKit browser, and Chromium or CHROME_EXECUTABLE.
 * BASE_URL defaults to http://127.0.0.1:8765 (the local demo's /preview/ signs in).
 */
const { chromium, webkit, devices } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = process.env.BASE_URL || 'http://127.0.0.1:8765';
const output = process.env.LAYOUT_OUTPUT || '/tmp/observatory-mobile-layout';
const rectangle = element => {
    const r = element.getBoundingClientRect();
    return { top:r.top, bottom:r.bottom, left:r.left, right:r.right, width:r.width, height:r.height };
};

async function geometry(page) {
    return page.evaluate(() => {
        const box = element => {
            const r = element.getBoundingClientRect();
            return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height};
        };
        const visible = element => getComputedStyle(element).display !== 'none';
        const top = document.querySelector('.navbar'), bottom = document.querySelector('.mobile-nav');
        return {
            width:innerWidth, height:innerHeight, scrollWidth:document.documentElement.scrollWidth,
            topVisible:visible(top), bottomVisible:visible(bottom), topPosition:getComputedStyle(top).position,
            top:box(top), bottom:box(bottom),
            brand:box(document.querySelector('.navbar .brand')), desktopLinks:box(document.querySelector('.nav-links')),
            links:[...document.querySelectorAll('.mobile-nav-inner > a, .mobile-nav-inner > button')].map(box),
            touch:matchMedia('(hover:none) and (pointer:coarse)').matches,
            cardValues:[...document.querySelectorAll('.metric-value')].map(element=>({value:box(element), card:box(element.closest('.metric-card'))})),
        };
    });
}

function checkGeometry(layout, name) {
    assert.ok(layout.scrollWidth <= layout.width + 1, `${name}: page overflows horizontally (${layout.scrollWidth}/${layout.width})`);
    const mobile = layout.width <= 767 || layout.touch;
    assert.equal(layout.bottomVisible, mobile, `${name}: bottom navigation mode`);
    assert.equal(layout.topVisible, !mobile, `${name}: top navigation mode`);
    if (mobile) {
        assert.ok(layout.bottom.top >= 0 && layout.bottom.bottom <= layout.height + 1, `${name}: bottom bar is outside viewport`);
        layout.links.forEach((r,index)=>{
            assert.ok(r.height >= 44 && r.width >= 44, `${name}: touch target ${index} is too small (${r.width}x${r.height})`);
            assert.ok(r.left >= -1 && r.right <= layout.width+1, `${name}: clipped navigation item ${index}`);
            if (index) assert.ok(r.left >= layout.links[index-1].right-1, `${name}: overlapping navigation items`);
        });
    } else {
        assert.equal(layout.topPosition, 'relative', `${name}: header must not stick beneath browser chrome`);
        assert.ok(layout.top.top >= 0, `${name}: top header clipped at page start`);
        assert.ok(layout.brand.left >= 0 && layout.brand.right <= layout.width, `${name}: brand clipped`);
        assert.ok(layout.brand.right <= layout.desktopLinks.left, `${name}: brand overlaps desktop navigation`);
    }
    layout.cardValues.forEach(({value,card})=>assert.ok(value.right <= card.right+1, `${name}: metric overflows its card`));
}

async function testProfile(browser, engine, name, options, screenshots) {
    const context = await browser.newContext({...options, colorScheme:'light'});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error=>errors.push(error.message));
    try {
        await page.goto(base+'/preview/', {waitUntil:'domcontentloaded'});
        await page.waitForFunction(()=>document.getElementById('refresh-state')?.textContent.startsWith('Updated'));
        let layout = await geometry(page);
        checkGeometry(layout, name);
        const prefix = layout.bottomVisible ? 'mobile' : 'desktop';
        const adminToggle = page.locator(`#${prefix}-admin-toggle`);
        const adminMenu = page.locator(`#${prefix}-admin-menu`);
        if (!layout.bottomVisible) {
            await adminToggle.hover();
            assert.equal(await adminToggle.getAttribute('aria-expanded'), 'true');
            await page.mouse.move(0, 300);
            assert.equal(await adminMenu.isVisible(), false, `${name}: hover menu did not close`);
        }
        await adminToggle.click();
        assert.equal(await adminToggle.getAttribute('aria-expanded'), 'true');
        const menuBox = await adminMenu.evaluate(rectangle);
        assert.ok(menuBox.left >= 0 && menuBox.right <= layout.width && menuBox.top >= 0 && menuBox.bottom <= layout.height, `${name}: Admin menu outside screen`);
        const items = await adminMenu.locator('a').evaluateAll(nodes => nodes.map(node => {
            const r=node.getBoundingClientRect(), parent=node.parentElement.getBoundingClientRect(), style=getComputedStyle(node);
            return {height:r.height,left:r.left-parent.left,right:parent.right-r.right,padding:parseFloat(style.paddingLeft)};
        }));
        assert.equal(items.length, 4);
        items.forEach(item => {
            assert.ok(item.height >= 44, `${name}: Admin item too short`);
            assert.ok(item.left >= 8 && item.right >= 8 && item.padding >= 12, `${name}: Admin text too close to menu edge`);
        });
        if (screenshots) await page.screenshot({path:path.join(output,`${engine}-${name.replaceAll(' ','-')}-admin.png`),animations:'disabled'});
        await adminToggle.press('Escape');
        assert.equal(await adminMenu.isVisible(), false);
        await adminToggle.press('ArrowDown');
        assert.equal(await adminMenu.isVisible(), true);
        assert.equal(await adminMenu.locator('a').first().evaluate(node=>node===document.activeElement), true);
        await adminMenu.locator('a').first().press('Escape');
        assert.equal(await adminMenu.isVisible(), false);
        await adminToggle.click();
        await page.locator('h1').click();
        assert.equal(await adminMenu.isVisible(), false, `${name}: outside click did not close Admin`);
        if (layout.bottomVisible) {
            await page.locator('#mobile-admin-toggle').click();
            assert.equal(await page.locator('#mobile-admin-toggle').getAttribute('aria-expanded'), 'true');
            const menu = await page.locator('#mobile-admin-menu').evaluate(rectangle);
            assert.ok(menu.top >= 0 && menu.bottom <= layout.bottom.top+1 && menu.left >= 0 && menu.right <= layout.width, `${name}: menu overlaps bar or screen edge`);
            await page.locator('#mobile-admin-toggle').press('Escape');
            assert.equal(await page.locator('#mobile-admin-menu').isVisible(), false);

            // Notch and home-indicator insets must be applied exactly once.
            const landscape = layout.width > layout.height;
            await page.addStyleTag({content:`:root {--safe-area-top:${landscape?0:47}px;--safe-area-bottom:${landscape?21:34}px;--safe-area-left:${landscape?47:0}px;--safe-area-right:${landscape?47:0}px;}`});
            const safe = await geometry(page);
            assert.equal(Math.round(safe.bottom.height-layout.bottom.height), landscape?21:34, `${name}: safe area counted twice`);
            assert.ok(safe.links[0].left >= (landscape?47:0), `${name}: left notch clips nav`);
            assert.ok(safe.links.at(-1).right <= safe.width-(landscape?47:0)+1, `${name}: right notch clips nav`);
            await page.evaluate(()=>window.scrollTo(0,document.documentElement.scrollHeight));
            const caption = await page.locator('.table-caption').evaluate(rectangle);
            const bottom = await page.locator('.mobile-nav').evaluate(rectangle);
            assert.ok(caption.bottom <= bottom.top, `${name}: final content hidden behind bar`);
            await page.evaluate(()=>window.scrollTo(0,0));
        } else {
            await page.evaluate(()=>window.scrollTo(0,400));
            assert.ok((await page.locator('.navbar').evaluate(rectangle)).bottom <= 0, `${name}: header remains pinned beneath toolbar`);
            await page.evaluate(()=>window.scrollTo(0,0));
        }
        if (screenshots) await page.screenshot({path:path.join(output,`${engine}-${name.replaceAll(' ','-')}.png`),animations:'disabled'});
        await page.locator(layout.bottomVisible?'.theme-toggle-mobile':'.theme-toggle').click();
        assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
        checkGeometry(await geometry(page), name+' dark');
        await adminToggle.click();
        assert.equal(await adminMenu.isVisible(), true);
        if (screenshots) await page.screenshot({path:path.join(output,`${engine}-${name.replaceAll(' ','-')}-admin-dark.png`),animations:'disabled'});
        await adminToggle.press('Escape');
        if (screenshots) await page.screenshot({path:path.join(output,`${engine}-${name.replaceAll(' ','-')}-dark.png`),animations:'disabled'});
        await page.goto(base+'/recordings/', {waitUntil:'domcontentloaded'});
        checkGeometry(await geometry(page), name+' recordings');
        await page.goto(base+'/', {waitUntil:'domcontentloaded'});
        checkGeometry(await geometry(page), name+' live');
        assert.deepEqual(errors, [], `${name}: JavaScript errors`);
        return {engine,profile:name,width:options.viewport.width,height:options.viewport.height,themes:['light','dark'],pages:['climate','recordings','live'],result:'passed'};
    } finally { await context.close(); }
}

async function testViewportChanges(browser, engine) {
    const context = await browser.newContext({...devices['iPhone 15 Pro'],colorScheme:'light'});
    const page = await context.newPage();
    try {
        await page.addInitScript(()=>{
            const viewport = new EventTarget();
            Object.assign(viewport,{height:innerHeight,width:innerWidth,offsetTop:0,offsetLeft:0,scale:1});
            Object.defineProperty(window,'visualViewport',{value:viewport,configurable:true});
            window.layoutTestViewport=viewport;
        });
        await page.goto(base+'/preview/',{waitUntil:'domcontentloaded'});
        await page.waitForFunction(()=>document.getElementById('refresh-state')?.textContent.startsWith('Updated'));
        await page.evaluate(()=>{
            const v=window.layoutTestViewport;
            v.height=innerHeight-54; v.offsetTop=24; v.dispatchEvent(new Event('resize'));
        });
        await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--viewport-bottom-offset')==='30px');
        const bar=await page.locator('.mobile-nav').evaluate(rectangle);
        const visibleBottom=await page.evaluate(()=>visualViewport.offsetTop+visualViewport.height);
        assert.ok(bar.bottom<=visibleBottom+1, `${engine}: toolbar obscures bottom navigation`);
        await page.evaluate(()=>{
            const input=document.createElement('input'); input.type='text'; document.body.append(input); input.focus();
            const v=window.layoutTestViewport;v.height=innerHeight-300;v.offsetTop=0;v.dispatchEvent(new Event('resize'));
        });
        await page.waitForFunction(()=>document.documentElement.classList.contains('keyboard-open'));
        assert.equal(await page.locator('.mobile-nav').isVisible(),false, `${engine}: nav overlays on-screen keyboard`);
        await page.evaluate(()=>{
            document.activeElement.blur(); const v=window.layoutTestViewport;v.height=innerHeight;v.offsetTop=0;v.dispatchEvent(new Event('resize'));
        });
        await page.waitForFunction(()=>!document.documentElement.classList.contains('keyboard-open'));
        assert.equal(await page.locator('.mobile-nav').isVisible(),true);
        return {engine,profile:'browser-toolbar-and-keyboard',result:'passed'};
    } finally { await context.close(); }
}

async function testDefaultTheme(browser, engine) {
    const context = await browser.newContext({viewport:{width:1024,height:768}, colorScheme:'dark'});
    const page = await context.newPage();
    try {
        await page.goto(base+'/preview/', {waitUntil:'domcontentloaded'});
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'light', `${engine}: dark OS must not override the white default`);
        assert.equal(await page.locator('body').evaluate(element=>getComputedStyle(element).backgroundColor), 'rgb(255, 255, 255)');
        await page.locator('.theme-toggle').click();
        await page.reload({waitUntil:'domcontentloaded'});
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark', `${engine}: explicit dark choice must persist`);
        await context.addCookies([{name:'theme',value:'invalid',url:base}]);
        await page.reload({waitUntil:'domcontentloaded'});
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'light', `${engine}: invalid cookie must fall back to light`);
        await context.clearCookies();
        await page.emulateMedia({colorScheme:'light'});
        await page.goto(base+'/login/', {waitUntil:'domcontentloaded'});
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
        return {engine,profile:'white-default-and-saved-theme',result:'passed'};
    } finally { await context.close(); }
}

(async()=>{
    await fs.mkdir(output,{recursive:true});
    const results=[];
    for (const [engine, launcher, names] of [
        ['webkit',webkit,['iPhone SE','iPhone SE (3rd gen)','iPhone 15 Pro','iPhone 15 Pro landscape','iPad Mini','iPad Mini landscape','iPad Pro 11','iPad Pro 11 landscape']],
        ['chromium',chromium,['Galaxy S9+','Galaxy S9+ landscape','Pixel 7','Pixel 7 landscape','Galaxy Tab S4','Galaxy Tab S4 landscape','Galaxy Tab S9','Galaxy Tab S9 landscape']],
    ]) {
        const browser=await launcher.launch(engine==='chromium' && process.env.CHROME_EXECUTABLE ? {executablePath:process.env.CHROME_EXECUTABLE} : {});
        results.push(await testDefaultTheme(browser, engine));
        console.log(`PASS ${engine}: white default and saved theme`);
        try {
            for (const name of names) {
                results.push(await testProfile(browser,engine,name,devices[name],['iPhone 15 Pro','iPhone 15 Pro landscape','iPad Pro 11 landscape','Pixel 7'].includes(name)));
                console.log(`PASS ${engine}: ${name}`);
            }
            for (const width of [320,360,640,641,767,768,1024,1512]) {
                const name=`window-${width}`;
                results.push(await testProfile(browser,engine,name,{viewport:{width,height:900},isMobile:false,hasTouch:false},width===1512));
                console.log(`PASS ${engine}: ${name}`);
            }
            results.push(await testViewportChanges(browser,engine));
            console.log(`PASS ${engine}: dynamic toolbar, keyboard dismissal`);
        } finally { await browser.close(); }
    }
    await fs.writeFile(path.join(output,'results.json'),JSON.stringify(results,null,2));
    console.log(`PASS: ${results.length} scenarios. Screenshots and results: ${output}`);
})().catch(error=>{console.error(error);process.exitCode=1;});

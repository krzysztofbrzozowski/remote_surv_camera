/* Environmental dashboard. No external chart dependency; measurements stay on this server. */
(() => {
    'use strict';
    const app = document.getElementById('climate-app');
    if (!app) return;
    const $ = id => document.getElementById(id);
    const params = new URLSearchParams(location.search);
    const ranges = ['1h', '24h', '7d', '10d', '30d'];
    const metrics = {
        temperature: {label:'Temperature', unit:'°C', digits:1},
        humidity: {label:'Humidity', unit:'%', digits:1},
        pressure: {label:'Pressure', unit:'hPa', digits:1},
        battery_voltage: {label:'Battery', unit:'V', digits:2}
    };
    let selected = params.get('sensor') || '';
    let range = ranges.includes(params.get('range')) ? params.get('range') : '24h';
    let metric = 'temperature';
    let state = null;
    let controller = null;
    let chartPoints = [];
    let plot = null;
    let zone = 'Europe/Warsaw';
    const number = (value, digits=1) => value == null ? '—' : new Intl.NumberFormat('en-GB', {
        minimumFractionDigits:digits, maximumFractionDigits:digits
    }).format(value);
    const valueWithUnit = (value, meta) => value == null ? '—' : `${number(value, meta.digits)} ${meta.unit}`;
    const date = (stamp, options={}) => new Intl.DateTimeFormat('en-GB', {
        timeZone:zone, day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', ...options
    }).format(new Date(stamp));
    const hour = stamp => new Intl.DateTimeFormat('en-GB', {timeZone:zone, hour:'2-digit', hourCycle:'h23'}).format(new Date(stamp));
    const element = (tag, className, text) => {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text != null) el.textContent = text;
        return el;
    };

    function updateButtons() {
        document.querySelectorAll('[data-range]').forEach(button => {
            const active = button.dataset.range === range;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        });
        document.querySelectorAll('[data-metric]').forEach(button => {
            const active = button.dataset.metric === metric;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        });
    }

    function syncURL() {
        const url = new URL(location.href);
        if (selected) url.searchParams.set('sensor', selected);
        else url.searchParams.delete('sensor');
        url.searchParams.set('range', range);
        history.replaceState(null, '', url);
        const csv = new URL(app.dataset.export, location.origin);
        csv.searchParams.set('sensor', selected);
        csv.searchParams.set('range', range);
        $('export-button').href = csv.toString();
    }

    async function refresh() {
        if (controller) controller.abort();
        const current = new AbortController();
        controller = current;
        const timeout = setTimeout(() => current.abort('timeout'), 15000);
        $('refresh-state').textContent = 'Updating…';
        $('refresh-button').disabled = true;
        const url = new URL(app.dataset.api, location.origin);
        url.searchParams.set('range', range);
        if (selected) url.searchParams.set('sensor', selected);
        try {
            const response = await fetch(url, {signal:current.signal, cache:'no-store', headers:{Accept:'application/json'}});
            if (response.redirected || response.status === 401 || response.status === 403) {
                throw new Error('Your session has expired. Sign in again to view readings.');
            }
            if (!response.ok) throw new Error(response.status === 404 ? 'This sensor is not available to your account.' : 'Could not load readings. Please try again.');
            const next = await response.json();
            if (controller !== current) return;
            state = next;
            selected = next.selected || '';
            zone = next.timezone;
            $('climate-error').hidden = true;
            $('refresh-state').textContent = `Updated ${date(next.end, {day:undefined, month:undefined, second:'2-digit'})}`;
            render();
            syncURL();
        } catch (error) {
            if (controller !== current) return;
            if (error.name === 'AbortError' && current.signal.reason !== 'timeout') return;
            $('climate-error').textContent = current.signal.reason === 'timeout'
                ? 'The server did not respond. Try refreshing the readings.' : error.message;
            $('climate-error').hidden = false;
            $('refresh-state').textContent = 'No connection · data may be outdated';
        } finally {
            clearTimeout(timeout);
            if (controller === current) { controller = null; $('refresh-button').disabled = false; }
        }
    }

    function renderSensors() {
        const list = $('sensor-list');
        // Reuse buttons during periodic refresh so keyboard focus stays in place.
        const existing = new Map(Array.from(list.children).map(button => [button.dataset.sensor, button]));
        for (const sensor of state.sensors) {
            let button = existing.get(sensor.id);
            if (!button) {
                button = element('button', 'sensor-choice');
                button.type = 'button';
                button.dataset.sensor = sensor.id;
                button.append(element('span', 'status-dot'), element('span', 'sensor-text'), element('span', 'sensor-temp'));
                button.children[1].append(element('strong'), element('small'));
                button.addEventListener('click', () => { selected = sensor.id; refresh(); });
                list.append(button);
            }
            button.classList.toggle('active', sensor.id === selected);
            button.setAttribute('aria-pressed', String(sensor.id === selected));
            button.children[0].className = `status-dot ${sensor.online ? 'online' : 'offline'}`;
            button.children[1].children[0].textContent = sensor.name;
            button.children[1].children[1].textContent = sensor.location || (sensor.online ? 'Connected' : 'Offline');
            button.children[2].textContent = valueWithUnit(sensor.temperature, metrics.temperature);
            button.setAttribute('aria-label', `${sensor.name}, ${sensor.online ? 'connected' : 'offline'}, ${valueWithUnit(sensor.temperature, metrics.temperature)}`);
            existing.delete(sensor.id);
        }
        existing.forEach(button => button.remove());
    }

    function render() {
        renderSensors();
        updateButtons();
        const sensor = state.sensors.find(item => item.id === selected);
        $('climate-empty').hidden = Boolean(sensor);
        $('climate-content').hidden = !sensor;
        if (!sensor) return;
        const latest = state.latest;
        $('sensor-name').textContent = sensor.name;
        $('sensor-location').textContent = sensor.location || 'Environmental sensor';
        $('sensor-status').textContent = !sensor.active ? 'Disabled' : sensor.online ? '● Connected' : '○ Offline';
        $('current-temperature').textContent = number(latest?.temperature, 1);
        $('current-humidity').textContent = number(latest?.humidity, 1);
        $('current-pressure').textContent = number(latest?.pressure, 1);
        $('current-battery').textContent = number(latest?.battery_voltage, 2);
        const age = latest ? (new Date(state.end) - new Date(latest.timestamp)) / 1000 : Infinity;
        $('reading-age').textContent = latest ? `${age > state.interval * 3 ? 'Last reading · ' : 'Reading · '}${date(latest.timestamp)}` : 'Waiting for the first reading';
        $('device-seen').textContent = sensor.last_seen ? date(sensor.last_seen) : 'Not yet';
        $('device-heading').textContent = latest ? 'Sensor status' : 'Waiting for a reading';
        $('device-interval').textContent = state.interval < 60 ? `${state.interval} s` : `${number(state.interval / 60, 1)} min`;
        $('device-message').textContent = !sensor.active ? 'The sensor is disabled. Its saved history is still available.' : sensor.online
            ? 'The sensor is sending data. Every received reading is saved to history.'
            : 'The sensor has missed three sampling intervals. Check its power and Wi-Fi. Previous readings remain in history.';
        $('chart-timezone').textContent = `Local time · ${zone}`;
        $('chart-resolution').textContent = ['1h','24h'].includes(range) ? 'Minute averages' : 'Hourly averages';
        drawChart();
        renderDaily();
        renderTable();
    }

    function drawChart() {
        if (!state || !selected) return;
        const meta = metrics[metric];
        ['min','avg','max'].forEach(kind => { $(`chart-${kind}`).textContent = valueWithUnit(state.stats[`${metric}_${kind}`], meta); });
        $('chart-count').textContent = number(state.stats.count || 0, 0);
        $('chart-legend').textContent = meta.label;
        $('chart-tooltip').hidden = true;
        const svg = $('history-chart');
        svg.replaceChildren();
        const NS = 'http://www.w3.org/2000/svg';
        function add(tag, attrs, text, parent=svg) {
            const node = document.createElementNS(NS, tag);
            Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key, String(value)));
            if (text != null) node.textContent = text;
            parent.append(node);
            return node;
        }
        add('title', {id:'chart-title'}, `History: ${meta.label}`);
        add('desc', {id:'chart-description'}, `${meta.label}, range ${range}. ${state.stats.count || 0} readings. Use the slider below the chart to inspect points. Recent readings are also available in the table below.`);
        const colors = getComputedStyle(document.documentElement);
        const accent = colors.getPropertyValue('--accent').trim();
        const grid = colors.getPropertyValue('--chart-grid').trim();
        const muted = colors.getPropertyValue('--text-tertiary').trim();
        const W=Math.max(260, svg.getBoundingClientRect().width);
        const compact=W<500, H=compact?220:260, left=compact?44:62, right=14, top=15, bottom=34;
        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        const start = new Date(state.start).getTime(), end = new Date(state.end).getTime();
        chartPoints = state.series.filter(row => row[metric] != null).map(row => ({time:new Date(row.timestamp).getTime(), value:row[metric]}));
        const values = chartPoints.map(point => point.value);
        let min = values.length ? Math.min(...values) : 0;
        let max = values.length ? Math.max(...values) : 1;
        const padding = Math.max((max - min) * .2, metric === 'battery_voltage' ? .05 : .5);
        min -= padding; max += padding;
        const x = time => left + (Math.max(start,time)-start)/(end-start)*(W-left-right);
        const y = value => top + (max-value)/(max-min)*(H-top-bottom);
        for (let i=0; i<5; i++) {
            const yy = top + (H-top-bottom)*i/4;
            add('line', {x1:left,y1:yy,x2:W-right,y2:yy,stroke:grid,'stroke-width':1,'stroke-dasharray':'3 5'});
            add('text', {x:left-12,y:yy+4,fill:muted,'text-anchor':'end','font-size':11,'font-family':'inherit'}, number(max-(max-min)*i/4, meta.digits));
        }
        const timeTicks=compact?3:5;
        for (let i=0; i<timeTicks; i++) {
            const stamp = start+(end-start)*i/(timeTicks-1);
            add('text', {x:x(stamp),y:H-6,fill:muted,'text-anchor':i===0?'start':i===timeTicks-1?'end':'middle','font-size':11,'font-family':'inherit'},
                ['1h','24h'].includes(range) ? date(stamp,{day:undefined,month:undefined}) : date(stamp,{hour:undefined,minute:undefined}));
        }
        $('chart-empty').hidden = chartPoints.length > 0;
        $('chart-cursor').max = Math.max(0, chartPoints.length-1);
        $('chart-cursor').disabled = !chartPoints.length;
        if (!chartPoints.length) { plot = null; return; }
        const defs = add('defs', {});
        const gradient = add('linearGradient', {id:'climate-fill', x1:0,y1:0,x2:0,y2:1}, null, defs);
        add('stop', {offset:'0%','stop-color':accent,'stop-opacity':'.16'}, null, gradient);
        add('stop', {offset:'100%','stop-color':accent,'stop-opacity':'.01'}, null, gradient);
        const allowedGap = ['1h','24h'].includes(range) ? Math.max(state.interval * 3, 180)*1000 : Math.max(state.interval * 3, 7200)*1000;
        let groups = [], current = [];
        chartPoints.forEach((point,index) => {
            if (index && point.time-chartPoints[index-1].time > allowedGap) { groups.push(current); current=[]; }
            current.push(point);
        });
        if (current.length) groups.push(current);
        groups.forEach(points => {
            const path = points.map((point,i) => `${i?'L':'M'}${x(point.time).toFixed(2)},${y(point.value).toFixed(2)}`).join(' ');
            if (points.length > 1) {
                add('path',{d:`${path} L${x(points.at(-1).time)},${H-bottom} L${x(points[0].time)},${H-bottom} Z`,fill:'url(#climate-fill)'});
                add('path',{d:path,fill:'none',stroke:accent,'stroke-width':2.2,'stroke-linecap':'round','stroke-linejoin':'round'});
            } else add('circle',{cx:x(points[0].time),cy:y(points[0].value),r:3,fill:accent});
        });
        const crosshair = add('line',{x1:0,y1:top,x2:0,y2:H-bottom,stroke:accent,'stroke-dasharray':'3 4',visibility:'hidden'});
        const dot = add('circle',{cx:0,cy:0,r:4,fill:accent,stroke:colors.getPropertyValue('--surface').trim(),'stroke-width':2,visibility:'hidden'});
        plot = {x,y,start,end,left,right,W,top,bottom,H,crosshair,dot};
    }

    function inspectPoint(index, announce=false) {
        if (!plot || !chartPoints[index]) return;
        const point = chartPoints[index], meta=metrics[metric];
        const px=plot.x(point.time), py=plot.y(point.value);
        plot.crosshair.setAttribute('x1',px); plot.crosshair.setAttribute('x2',px); plot.crosshair.setAttribute('visibility','visible');
        plot.dot.setAttribute('cx',px); plot.dot.setAttribute('cy',py); plot.dot.setAttribute('visibility','visible');
        const tooltip=$('chart-tooltip');
        tooltip.replaceChildren(element('span','',date(point.time)), element('strong','',valueWithUnit(point.value, meta)));
        tooltip.hidden=false;
        const svgRect=$('history-chart').getBoundingClientRect();
        // Account for SVG's default xMidYMid meet scaling on narrow screens.
        const scale=Math.min(svgRect.width/plot.W,svgRect.height/plot.H);
        const offsetX=(svgRect.width-plot.W*scale)/2;
        tooltip.style.left=`${Math.max(0,Math.min(svgRect.width-tooltip.offsetWidth,offsetX+px*scale+12))}px`;
        tooltip.style.top='16px';
        $('chart-cursor').value=index;
        $('chart-cursor').setAttribute('aria-valuetext',`${date(point.time)}, ${valueWithUnit(point.value,meta)}`);
        if (announce) $('chart-announcement').textContent=`${date(point.time)}, ${valueWithUnit(point.value,meta)}`;
    }

    $('history-chart').addEventListener('pointermove', event => {
        if (!plot || !chartPoints.length) return;
        const rect=event.currentTarget.getBoundingClientRect();
        const scale=Math.min(rect.width/plot.W,rect.height/plot.H);
        const offsetX=(rect.width-plot.W*scale)/2;
        const px=(event.clientX-rect.left-offsetX)/scale;
        const time=plot.start+(px-plot.left)/(plot.W-plot.left-plot.right)*(plot.end-plot.start);
        let closest=0;
        chartPoints.forEach((point,i) => { if (Math.abs(point.time-time)<Math.abs(chartPoints[closest].time-time)) closest=i; });
        inspectPoint(closest);
    });
    $('history-chart').addEventListener('pointerleave', () => {
        $('chart-tooltip').hidden=true;
        if (plot) { plot.crosshair.setAttribute('visibility','hidden'); plot.dot.setAttribute('visibility','hidden'); }
    });
    $('chart-cursor').addEventListener('input', event => inspectPoint(Number(event.target.value),true));
    $('chart-cursor').addEventListener('focus', event => inspectPoint(Number(event.target.value),true));

    function renderDaily() {
        const grid=$('daily-grid'); grid.replaceChildren();
        const temperatures=state.hourly.map(row=>row.temperature);
        const min=temperatures.length?Math.min(...temperatures):0, max=temperatures.length?Math.max(...temperatures):1;
        const hourly=new Map(state.hourly.map(row=>[new Date(row.timestamp).getTime(), row.temperature]));
        const end=new Date(state.end); end.setUTCMinutes(0,0,0);
        for (let i=23;i>=0;i--) {
            const time=end.getTime()-i*3600000, value=hourly.get(time);
            const cell=element('div','hour-cell'), tile=element('div','hour-tile');
            if (value != null) {
                const ratio=max===min ? .5 : (value-min)/(max-min);
                tile.style.background=`hsl(210 64% ${90-ratio*45}%)`;
            }
            cell.title=`${date(time)} · ${value == null?'No data':valueWithUnit(value,metrics.temperature)}`;
            cell.setAttribute('aria-label',cell.title);
            cell.append(tile,element('span','',hour(time)));
            grid.append(cell);
        }
    }

    function renderTable() {
        const body=$('readings-body'); body.replaceChildren();
        state.recent.forEach(row => {
            const tr=element('tr');
            tr.append(element('td','',date(row.timestamp,{second:'2-digit'})));
            Object.keys(metrics).forEach(key=>tr.append(element('td','',valueWithUnit(row[key],metrics[key]))));
            body.append(tr);
        });
        if (!state.recent.length) {
            const cell=element('td','','No readings in the selected period.'); cell.colSpan=5;
            const row=element('tr'); row.append(cell); body.append(row);
        }
    }

    $('range-options').addEventListener('click', event => {
        const button=event.target.closest('[data-range]');
        if (!button) return;
        range=button.dataset.range; updateButtons(); refresh();
    });
    $('metric-options').addEventListener('click', event => {
        const button=event.target.closest('[data-metric]');
        if (!button) return;
        metric=button.dataset.metric; updateButtons(); drawChart();
    });
    $('refresh-button').addEventListener('click',refresh);
    const previousThemeCallback=window._onThemeChange;
    window._onThemeChange=theme=>{ if(previousThemeCallback) previousThemeCallback(theme); drawChart(); };
    document.addEventListener('visibilitychange',()=>{ if(!document.hidden) refresh(); });
    let resizeTimer;
    window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(drawChart,100);});
    setInterval(()=>{ if(!document.hidden && !controller) refresh(); },30000);
    updateButtons(); refresh();
})();

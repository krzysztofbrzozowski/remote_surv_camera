# Navigation and mobile layout checks

The top navigation stays in normal document flow. This avoids relying on
sticky positioning and backdrop-filter compositing next to Safari's browser
toolbar. Small windows and touch devices, including landscape phones and
tablets, use the bottom navigation instead.

Safe-area insets are applied once to the bottom bar, with corresponding
content padding so the last row can scroll above it. Side insets protect
controls in landscape. The Visual Viewport API adjusts the bottom position
when browser controls obscure part of the viewport; navigation is hidden
while an editable field and a reduced viewport indicate an open keyboard.
Without that API, the CSS layout and safe-area handling remain available.
Inputs use at least 16 px text on mobile to avoid focus-induced zoom.

The common CSS and navigation script use versioned URLs, so existing browser
caches pick up this change despite the proxy's long static-file cache.
Change the URL version when publishing another update to these assets.

## Automated checks

`tests/mobile_layout.cjs` uses Playwright's WebKit and Chromium engines against
the isolated local demo at `http://127.0.0.1:8765`. That demo's `/preview/`
endpoint signs in to its separate database; the endpoint is not part of the
application deployed on the LAN or in production. Set `BASE_URL` only to
another isolated demo with the same entry point.

Install Playwright and its browsers in a temporary directory, then run:

```sh
npm install --prefix /tmp/observatory-browser-tools playwright
PLAYWRIGHT_BROWSERS_PATH=/tmp/observatory-browsers \
  /tmp/observatory-browser-tools/node_modules/.bin/playwright install webkit chromium
NODE_PATH=/tmp/observatory-browser-tools/node_modules \
  PLAYWRIGHT_BROWSERS_PATH=/tmp/observatory-browsers \
  node tests/mobile_layout.cjs
```

Set `CHROME_EXECUTABLE` to use an installed Chrome binary instead of the
downloaded Chromium. Set `LAYOUT_OUTPUT` to choose the screenshot/report folder.

The 36 scenarios cover iPhone SE and 15 Pro, iPad Mini and Pro, Galaxy phones
and tablets, Pixel 7, portrait and landscape, and window widths from 320 to
1512 px. Checks include both themes, Climate/Recordings/Live pages, horizontal
overflow, touch targets, Admin popup bounds and Escape, content clearance,
notch/home-indicator insets, changing viewport offsets and keyboard dismissal.
The latter conditions use injected safe-area values and viewport events.
They also check that a dark system appearance still starts with a white page,
that a manually selected dark theme survives reload, and that an invalid theme
cookie falls back to light.

Admin menus reserve an 8 px outer inset and at least 12 px inside each link,
with rows at least 44 px tall. The menu checks measure these actual dimensions
in desktop and touch layouts, including light/dark screenshots. Desktop Admin
supports hover, click and keyboard activation; both layouts support Arrow Down,
Escape and closing on outside click or focus movement.

These are automated browser-engine and device-profile tests. They cannot
reproduce every hardware/browser/OS combination or Safari's actual system
toolbar. Physical-device verification should include scrolling with the
address bar expanded and collapsed, rotating, opening Admin, and opening and
closing the keyboard on the login page.

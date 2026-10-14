/* Keep the bottom navigation inside the visible viewport as browser chrome changes. */
(() => {
    'use strict';
    const root = document.documentElement;
    const viewport = window.visualViewport;
    let frame;

    function updateViewport() {
        frame = null;
        if (!viewport || Math.abs(viewport.scale - 1) > .01) {
            root.style.setProperty('--viewport-bottom-offset', '0px');
            root.classList.remove('keyboard-open');
            return;
        }
        const obscured = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
        const focused = document.activeElement;
        const editing = focused && (focused.matches('input:not([type="range"]), textarea, select') || focused.isContentEditable);
        const keyboardOpen = Boolean(editing && window.innerHeight - viewport.height > 150);
        root.classList.toggle('keyboard-open', keyboardOpen);
        root.style.setProperty('--viewport-bottom-offset', `${keyboardOpen ? 0 : Math.ceil(obscured)}px`);
    }

    function scheduleUpdate() {
        if (frame == null) frame = requestAnimationFrame(updateViewport);
    }

    if (viewport) {
        viewport.addEventListener('resize', scheduleUpdate);
        viewport.addEventListener('scroll', scheduleUpdate);
    }
    window.addEventListener('resize', scheduleUpdate);
    window.addEventListener('pageshow', scheduleUpdate);
    document.addEventListener('focusin', scheduleUpdate);
    document.addEventListener('focusout', scheduleUpdate);
    updateViewport();

    function setupAdminMenu(prefix) {
        const toggle = document.getElementById(`${prefix}-admin-toggle`);
        const menu = document.getElementById(`${prefix}-admin-menu`);
        if (!toggle || !menu) return;
        const wrapper = prefix === 'desktop' ? toggle.closest('.nav-dropdown') : null;
        let openedByHover = false;
        function setOpen(open) {
            menu.classList.toggle('open', open);
            toggle.setAttribute('aria-expanded', String(open));
            if (!open) openedByHover = false;
        }
        toggle.addEventListener('click', event => {
            event.preventDefault();
            setOpen(openedByHover || !menu.classList.contains('open'));
            openedByHover = false;
        });
        toggle.addEventListener('keydown', event => {
            if (event.key === ' ' && toggle.tagName !== 'BUTTON') { event.preventDefault(); toggle.click(); }
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                setOpen(true);
                menu.querySelector('a')?.focus();
            }
        });
        if (wrapper) {
            wrapper.addEventListener('pointerenter', event => {
                if (event.pointerType === 'mouse' && !menu.classList.contains('open')) {
                    setOpen(true);
                    openedByHover = true;
                }
            });
            wrapper.addEventListener('pointerleave', event => {
                if (event.pointerType === 'mouse' && !wrapper.contains(document.activeElement)) setOpen(false);
            });
        }
        document.addEventListener('click', event => {
            if (!menu.contains(event.target) && !toggle.contains(event.target)) setOpen(false);
        });
        document.addEventListener('focusin', event => {
            if (!menu.contains(event.target) && !toggle.contains(event.target)) setOpen(false);
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && menu.classList.contains('open')) { setOpen(false); toggle.focus(); }
        });
        window.addEventListener('resize', () => setOpen(false));
    }
    setupAdminMenu('desktop');
    setupAdminMenu('mobile');
})();

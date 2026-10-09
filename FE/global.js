/* =====================================================================
   global.js — Logika bersama semua halaman P Care
   Muat SEBELUM script halaman, lalu panggil PCare.init().

   Atribut <body>:
     data-page="camera|performance|settings|log|login"
     data-title="KAMERA" (judul kecil di header mobile)
   Sidebar & menu mobile dibuat otomatis (kecuali halaman login).
===================================================================== */

const PCare = (() => {

  /* ---------- Ikon ---------- */
  const svg = (inner, cls = 'icon') =>
    `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;

  const ICON = {
    camera: '<path d="M4 8a2 2 0 0 1 2-2h2l1.2-1.6A2 2 0 0 1 10.8 3.6h2.4a2 2 0 0 1 1.6.8L16 6h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8Z"/><circle cx="12" cy="13" r="3.2"/>',
    performance: '<path d="M3 17 8 10l4 3 4-6 5 10"/><path d="M3 21h18"/>',
    log: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>',
    moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
    menu: '<path d="M3 6h18M3 12h18M3 18h18"/>'
  };
  const themeIcons = () =>
    svg(ICON.moon, 'icon icon-moon') + svg(ICON.sun, 'icon icon-sun hidden');

  /* ---------- Tema ---------- */
  const Theme = {
    KEY: 'pcare_theme',
    isDark() { return document.documentElement.classList.contains('dark'); },
    apply(dark) {
      document.documentElement.classList.toggle('dark', dark);
      try { localStorage.setItem(this.KEY, dark ? 'dark' : 'light'); } catch (e) {}
      document.querySelectorAll('.icon-moon').forEach(el => el.classList.toggle('hidden', dark));
      document.querySelectorAll('.icon-sun').forEach(el => el.classList.toggle('hidden', !dark));
      document.querySelectorAll('.theme-label').forEach(el => el.textContent = dark ? 'Mode terang' : 'Mode gelap');
      if (typeof window.updateChartColors === 'function') window.updateChartColors(dark);
    },
    toggle() { this.apply(!this.isDark()); },
    init() {
      let saved = null;
      try { saved = localStorage.getItem(this.KEY); } catch (e) {}
      this.apply(saved === 'dark');
      document.querySelectorAll('[data-theme-toggle]').forEach(b => b.addEventListener('click', () => this.toggle()));
    }
  };

  /* ---------- Shell: sidebar desktop + header & drawer mobile ---------- */
  const NAV = [
    { id: 'camera', href: '/', label: 'Kamera' },
    { id: 'performance', href: '/performance', label: 'Performa' },
    { id: 'log', href: '/log', label: 'Log' },
    { id: 'settings', href: '/settings', label: 'Pengaturan' }
  ];

  const Shell = {
    render() {
      const page = document.body.dataset.page;
      const layout = document.querySelector('.layout');
      if (!layout || page === 'login') return;
      const title = document.body.dataset.title || '';

      const links = (mobile) => NAV.map(n =>
        `<a href="${n.href}" class="nav-item${n.id === page ? ' is-current' : ''}"${n.id === page ? ' aria-current="page"' : ''}>
          ${svg(ICON[n.id])}<span class="${mobile ? '' : 'label'}">${n.label}</span></a>`).join('');

      const brand = (mobile) => `<div class="brand"><div class="brand-mark"></div><span class="brand-name${mobile ? '' : ' label'}">P Care</span></div>`;

      const bottom = (mobile) => `
        <button type="button" class="nav-item" data-theme-toggle>${themeIcons()}<span class="theme-label ${mobile ? '' : 'label'}">Mode gelap</span></button>
        <button type="button" class="nav-item danger" data-logout>${svg(ICON.logout)}<span class="${mobile ? '' : 'label'}">Keluar</span></button>`;

      layout.insertAdjacentHTML('afterbegin', `
        <aside id="sidebar" class="sidebar">
          <div>${brand(false)}<nav class="nav">${links(false)}</nav></div>
          <div class="nav-bottom">${bottom(false)}</div>
        </aside>`);

      document.body.insertAdjacentHTML('beforeend', `
        <header class="topbar">
          <button id="hamburgerBtn" class="icon-btn" aria-label="Buka menu" aria-expanded="false">${svg(ICON.menu, 'icon')}</button>
          <span class="topbar-title">P Care / ${title}</span>
          <span id="mobileConnectionStatus" class="dot ok live"></span>
        </header>
        <div id="mobileMenuOverlay" class="drawer hidden">
          <div id="mobileOverlayBg" class="drawer-bg"></div>
          <nav class="drawer-panel">
            <div>${brand(true)}${links(true)}</div>
            <div>${bottom(true)}</div>
          </nav>
        </div>`);
    }
  };

  /* ---------- Sidebar pin (klik dua kali) ---------- */
  const Sidebar = {
    init() {
      const s = document.getElementById('sidebar');
      if (s) s.addEventListener('dblclick', () => s.classList.toggle('pinned-open'));
    }
  };

  /* ---------- Menu mobile ---------- */
  const MobileMenu = {
    init() {
      const btn = document.getElementById('hamburgerBtn');
      const overlay = document.getElementById('mobileMenuOverlay');
      const bg = document.getElementById('mobileOverlayBg');
      if (!btn || !overlay || !bg) return;
      btn.addEventListener('click', () => { overlay.classList.remove('hidden'); btn.setAttribute('aria-expanded', 'true'); });
      bg.addEventListener('click', () => { overlay.classList.add('hidden'); btn.setAttribute('aria-expanded', 'false'); });
    }
  };

  /* ---------- Jam ---------- */
  const Clock = {
    init() {
      const el = document.getElementById('clock');
      if (!el) return;
      const tick = () => el.textContent = new Date().toLocaleTimeString('id-ID', { hour12: false });
      tick(); setInterval(tick, 1000);
    }
  };

  /* ---------- Helper ---------- */
  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  }

  async function saveSettingToServer(key, value) {
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value })
      });
      return res.ok;
    } catch (err) { return false; }
  }

  /* ---------- Grafik garis sederhana (canvas, pengganti Chart.js) ----------
     opts: { color:'--ok' (nama CSS variable), max, min, ticks:[..], tickFmt(v), points }  */
  class LineChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.opts = Object.assign({ color: '--ok', min: 0, max: null, ticks: null, tickFmt: v => v, points: 30 }, opts);
      this.data = Array(this.opts.points).fill(0);
      window.addEventListener('resize', () => this.draw());
      this.draw();
    }
    push(v) { this.data.push(v); this.data.shift(); this.draw(); }
    draw() {
      const c = this.canvas, o = this.opts;
      const w = c.clientWidth, h = c.clientHeight;
      if (!w || !h) return;
      const dpr = window.devicePixelRatio || 1;
      if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
        c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
      }
      const ctx = c.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const css = getComputedStyle(document.documentElement);
      const color = css.getPropertyValue(o.color).trim() || '#8B5A3C';
      const line = css.getPropertyValue('--line').trim();
      const muted = css.getPropertyValue('--muted').trim();

      const L = 44, R = 8, T = 8, B = 20;
      const pw = w - L - R, ph = h - T - B;
      const dataMax = Math.max(...this.data, 1);
      const max = o.max != null ? o.max : dataMax * 1.2;
      const min = o.min;
      const ticks = o.ticks || [0, 1, 2, 3, 4].map(i => min + (max - min) * i / 4);
      const yOf = v => T + ph - ((v - min) / (max - min)) * ph;
      const xOf = i => L + (i / (this.data.length - 1)) * pw;

      ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
      ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
      ticks.forEach(t => {
        const y = yOf(t);
        ctx.strokeStyle = line; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
        ctx.fillStyle = muted;
        ctx.fillText(String(o.tickFmt(Math.round(t))), L - 6, y);
      });

      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      const n = this.data.length;
      [0, 5, 10, 15, 20, 25, n - 1].forEach(i => {
        if (i >= n) return;
        ctx.fillStyle = muted;
        ctx.fillText((n - i) + 'd', Math.min(Math.max(xOf(i), L + 8), w - R - 8), h - 5);
      });

      ctx.beginPath();
      this.data.forEach((v, i) => {
        const y = Math.min(Math.max(yOf(v), T), T + ph);
        i === 0 ? ctx.moveTo(xOf(i), y) : ctx.lineTo(xOf(i), y);
      });
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
      ctx.lineTo(xOf(n - 1), T + ph); ctx.lineTo(xOf(0), T + ph); ctx.closePath();
      ctx.globalAlpha = 0.14; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
    }
  }

  /* ---------- Auth: logout + arahkan ke /login bila sesi habis (401) ---------- */
  const Auth = {
    init() {
      const nativeFetch = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const res = await nativeFetch(input, init);
        const url = typeof input === 'string' ? input : ((input && input.url) || '');
        if (res.status === 401 && !url.includes('/api/auth/') && document.body.dataset.page !== 'login') {
          window.location.href = '/login';
        }
        return res;
      };
      document.querySelectorAll('[data-logout]').forEach(b => b.addEventListener('click', async () => {
        try { await nativeFetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
        window.location.href = '/login';
      }));
    }
  };

  function init() {
    Shell.render();
    Auth.init();
    Theme.init();
    Sidebar.init();
    MobileMenu.init();
    Clock.init();
  }

  return { init, Auth, Theme, Shell, Sidebar, MobileMenu, Clock, LineChart, escapeHtml, saveSettingToServer };
})();
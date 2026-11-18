(() => {
  "use strict";

  const cfg = window.__PARADERO__ || {};
  const ROOT = cfg.rootPath || "";
  const DEFAULT_COLOR = "0EA5E9";
  const VIDEO_GRACE_MS = 5000;
  const FAILED_SKIP_MS = 1500;

  function tickClock() {
    const el = document.getElementById("clock");
    if (!el) return;
    el.textContent = new Date().toLocaleTimeString("es-PE", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }
  tickClock();
  setInterval(tickClock, 1000);

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function normalizeColor(raw) {
    const c = String(raw || "").replace("#", "").toUpperCase();
    if (/^[0-9A-F]{3}$/.test(c)) {
      return c.split("").map(function (ch) { return ch + ch; }).join("");
    }
    if (/^[0-9A-F]{6}$/.test(c) || /^[0-9A-F]{8}$/.test(c)) {
      return c.slice(0, 6);
    }
    return "";
  }

  const ROUTE_PALETTE = [
    "22D3EE", "F472B6", "A3E635", "FB923C", "818CF8", "FACC15",
    "2DD4BF", "F87171", "C084FC", "4ADE80", "38BDF8", "E879F9",
    "FBBF24", "94A3B8",
  ];
  const GENERIC_ROUTE_COLORS = {
    FEDD00: 1, FFFFFF: 1, "000000": 1, FFFF00: 1, FFD700: 1,
  };

  function hashRouteId(id) {
    const s = String(id || "");
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return h >>> 0;
  }

  function relativeLuminance(hex6) {
    const n = parseInt(hex6, 16);
    const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (v) {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
  }

  function inkFor(hex6) {
    return relativeLuminance(hex6) > 0.45 ? "0B1220" : "FFFFFF";
  }

  function pad2(n) {
    const s = String(n);
    return s.length < 2 ? "0" + s : s;
  }

  function hslToHex(h, s, l) {
    const a = (s / 100) * Math.min(l / 100, 1 - l / 100);
    function f(n) {
      const k = (n + h / 30) % 12;
      const color = l / 100 - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
      const v = Math.round(255 * Math.max(0, Math.min(1, color)));
      return pad2(v.toString(16));
    }
    return (f(0) + f(8) + f(4)).toUpperCase();
  }

  function paletteColorFor(routeId, used) {
    const start = hashRouteId(routeId) % ROUTE_PALETTE.length;
    for (let i = 0; i < ROUTE_PALETTE.length; i++) {
      const c = ROUTE_PALETTE[(start + i) % ROUTE_PALETTE.length];
      if (!used[c]) return c;
    }
    const h = hashRouteId(routeId) % 360;
    const s = 72 + (hashRouteId(routeId + ":s") % 20);
    const l = 52 + (hashRouteId(routeId + ":l") % 16);
    return hslToHex(h, s, l);
  }

  function assignDisplayColors(routes) {
    const list = Array.isArray(routes) ? routes : [];
    const freq = {};
    list.forEach(function (r) {
      const c = normalizeColor(r.route_color);
      if (!c) return;
      freq[c] = (freq[c] || 0) + 1;
    });
    const used = {};
    list.forEach(function (r) {
      const raw = normalizeColor(r.route_color);
      const keepOwn =
        raw &&
        !GENERIC_ROUTE_COLORS[raw] &&
        freq[raw] === 1 &&
        !used[raw];
      const hex = keepOwn ? raw : paletteColorFor(r.route_id, used);
      used[hex] = 1;
      r.route_color = hex;
      r._route_ink = inkFor(hex);
    });
    return list;
  }

  /* ---- Map (Leaflet) ---- */
  let map = null;
  let stopMarker = null;
  let stopLatLng = null;
  const routeLayers = {}; // route_id -> { line, hit }
  let selectedRouteId = null;
  const mapStatus = document.getElementById("map-status");
  let tilesOk = false;

  function scheduleInvalidate(delay) {
    setTimeout(function () {
      if (map) {
        try { map.invalidateSize(false); } catch (e) { /* ignore */ }
      }
    }, delay == null ? 80 : delay);
  }

  function ensureMap() {
    const el = document.getElementById("map");
    if (!el || typeof L === "undefined") return null;
    if (map) return map;

    map = L.map(el, {
      zoomControl: false,
      attributionControl: true,
      tap: true,
      tapTolerance: 28,
      touchZoom: true,
      bounceAtZoomLimits: false,
      preferCanvas: false,
    });
    L.control.zoom({ position: "topright" }).addTo(map);

    // Esri dark gray (sin API key). Fallback OSM FR si falla.
    const esriUrl =
      "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}";
    const fallbackUrl =
      "https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png";

    let usingFallback = false;
    const tiles = L.tileLayer(esriUrl, {
      attribution: "Tiles &copy; Esri",
      maxZoom: 16,
      errorTileUrl: "",
      crossOrigin: true,
    });
    tiles.on("tileerror", function () {
      if (usingFallback) {
        if (mapStatus) mapStatus.hidden = false;
        return;
      }
      usingFallback = true;
      if (mapStatus) {
        mapStatus.hidden = false;
        mapStatus.textContent = "Mapa: tiles alternativos";
      }
      try { map.removeLayer(tiles); } catch (e) { /* ignore */ }
      L.tileLayer(fallbackUrl, {
        attribution: "&copy; OSM",
        maxZoom: 19,
      }).addTo(map);
    });
    tiles.on("load", function () {
      tilesOk = true;
      if (mapStatus && !usingFallback) mapStatus.hidden = true;
    });
    tiles.addTo(map);

    const icon = L.icon({
      iconUrl: ROOT + "/static/paradero/vendor/leaflet/images/marker-icon.png",
      iconRetinaUrl: ROOT + "/static/paradero/vendor/leaflet/images/marker-icon-2x.png",
      shadowUrl: ROOT + "/static/paradero/vendor/leaflet/images/marker-shadow.png",
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowSize: [41, 41],
    });
    stopMarker = L.marker([-16.4, -71.54], { icon: icon });

    // ResizeObserver: WebView a menudo monta con altura 0
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(function () { scheduleInvalidate(30); });
      ro.observe(el);
    }
    scheduleInvalidate(0);
    scheduleInvalidate(200);
    scheduleInvalidate(600);
    return map;
  }

  function setStopMarker(stop) {
    if (!ensureMap() || !stop) return;
    const lat = Number(stop.stop_lat);
    const lon = Number(stop.stop_lon);
    if (!isFinite(lat) || !isFinite(lon)) return;
    stopLatLng = L.latLng(lat, lon);
    stopMarker.setLatLng(stopLatLng);
    if (!map.hasLayer(stopMarker)) stopMarker.addTo(map);
  }

  function clearRouteLayers() {
    Object.keys(routeLayers).forEach(function (id) {
      const g = routeLayers[id];
      if (map) {
        if (g.line && map.hasLayer(g.line)) map.removeLayer(g.line);
        if (g.hit && map.hasLayer(g.hit)) map.removeLayer(g.hit);
      }
    });
    Object.keys(routeLayers).forEach(function (id) { delete routeLayers[id]; });
  }

  function paintRouteLayers(routes) {
    if (!ensureMap()) return;
    clearRouteLayers();
    const bounds = [];
    (routes || []).forEach(function (r) {
      const shape = Array.isArray(r.shape) ? r.shape : [];
      if (shape.length < 2) return;
      const color = "#" + (normalizeColor(r.route_color) || DEFAULT_COLOR);
      const line = L.polyline(shape, {
        color: color,
        weight: 5,
        opacity: 0.7,
        lineJoin: "round",
        interactive: false,
      });
      // Área de toque ancha (invisible) encima
      const hit = L.polyline(shape, {
        color: color,
        weight: 28,
        opacity: 0,
        lineJoin: "round",
        interactive: true,
        bubblingMouseEvents: false,
      });
      const rid = String(r.route_id);
      hit.on("click", function (ev) {
        if (ev && ev.originalEvent) {
          L.DomEvent.stopPropagation(ev.originalEvent);
        }
        bumpActivity();
        selectRoute(rid);
      });
      line.addTo(map);
      hit.addTo(map);
      routeLayers[rid] = { line: line, hit: hit };
      shape.forEach(function (p) { bounds.push(p); });
    });
    if (stopMarker && map.hasLayer(stopMarker)) {
      bounds.push(stopMarker.getLatLng());
    }
    if (bounds.length) {
      try {
        map.fitBounds(bounds, { padding: [28, 28], maxZoom: 15 });
      } catch (e) { /* ignore */ }
    }
    applySelectionStyles();
    scheduleInvalidate(80);
  }

  function applySelectionStyles() {
    Object.keys(routeLayers).forEach(function (id) {
      const g = routeLayers[id];
      const selected = selectedRouteId && id === selectedRouteId;
      if (g.line) {
        g.line.setStyle({
          weight: selected ? 8 : 5,
          opacity: selectedRouteId ? (selected ? 1 : 0.18) : 0.7,
        });
        if (selected) g.line.bringToFront();
      }
      if (g.hit && selected) g.hit.bringToFront();
    });
    const cards = document.querySelectorAll(".route-card");
    for (let i = 0; i < cards.length; i++) {
      const card = cards[i];
      const id = card.getAttribute("data-route-id");
      if (selectedRouteId === id) card.classList.add("selected");
      else card.classList.remove("selected");
      if (selectedRouteId && selectedRouteId !== id) card.classList.add("dim");
      else card.classList.remove("dim");
    }
  }

  function fitAllRoutes() {
    const all = [];
    Object.keys(routeLayers).forEach(function (id) {
      const g = routeLayers[id];
      if (g.line) {
        g.line.getLatLngs().forEach(function (p) { all.push(p); });
      }
    });
    if (stopMarker && map && map.hasLayer(stopMarker)) all.push(stopMarker.getLatLng());
    if (all.length && map) {
      try {
        map.fitBounds(all, { padding: [28, 28], maxZoom: 15 });
      } catch (e) { /* ignore */ }
    }
  }

  function centerOnStop() {
    bumpActivity();
    if (!ensureMap()) return;
    if (stopLatLng) {
      map.setView(stopLatLng, Math.max(map.getZoom(), 15), { animate: true });
    } else {
      fitAllRoutes();
    }
    scheduleInvalidate(60);
  }

  function selectRoute(routeId) {
    if (!routeId) {
      selectedRouteId = null;
      applySelectionStyles();
      fitAllRoutes();
      return;
    }
    if (selectedRouteId === routeId) {
      selectRoute(null);
      return;
    }
    selectedRouteId = routeId;
    applySelectionStyles();
    const g = routeLayers[routeId];
    if (g && g.line && map) {
      try {
        map.fitBounds(g.line.getBounds(), { padding: [36, 36], maxZoom: 16 });
      } catch (e) { /* ignore */ }
    }
  }

  /* ---- Routes list ---- */
  function paintHeader(data) {
    const nameEl = document.getElementById("stop-name");
    const metaEl = document.getElementById("stop-meta");
    const countEl = document.getElementById("route-count");
    const stop = data.stop || {};
    const device = data.device || {};
    if (nameEl) nameEl.textContent = stop.stop_name || device.name || "Paradero";
    if (metaEl) {
      const parts = [];
      if (stop.district_name) parts.push(stop.district_name);
      if (device.name) parts.push(device.name);
      metaEl.textContent = parts.join(" · ");
    }
    if (countEl) countEl.textContent = String((data.routes || []).length);
  }

  function paintRoutesList(routes) {
    const routesEl = document.getElementById("routes");
    if (!routesEl) return;
    if (!Array.isArray(routes) || !routes.length) {
      routesEl.innerHTML =
        '<p class="empty">Sin rutas asignadas. Configura un <code>stop_id</code> en el admin.</p>';
      return;
    }
    routesEl.innerHTML = routes
      .map(function (r) {
        const color = normalizeColor(r.route_color) || DEFAULT_COLOR;
        const ink = r._route_ink || inkFor(color);
        const title =
          r.nombre_popular || r.route_long_name || r.route_short_name || r.route_id;
        const dest =
          r.desde && r.hasta
            ? r.desde + " → " + r.hasta
            : r.route_long_name || "";
        const meta = r.interval_min ? "cada " + r.interval_min + " min" : "";
        return (
          '<article class="route-card" data-route-id="' +
          escapeHtml(r.route_id) +
          '" style="--route:#' +
          color +
          ";--route-ink:#" +
          ink +
          '">' +
          '<div class="badge">' +
          escapeHtml(r.route_short_name || r.route_id) +
          "</div>" +
          '<div class="route-body">' +
          "<strong>" +
          escapeHtml(title) +
          "</strong>" +
          "<span>" +
          escapeHtml(dest) +
          "</span>" +
          "<em>" +
          escapeHtml(meta) +
          "</em>" +
          "</div></article>"
        );
      })
      .join("");
  }

  const routesRoot = document.getElementById("routes");
  if (routesRoot) {
    routesRoot.addEventListener("click", function (ev) {
      let node = ev.target;
      while (node && node !== routesRoot) {
        if (node.classList && node.classList.contains("route-card")) {
          const rid = node.getAttribute("data-route-id");
          if (rid) {
            bumpActivity();
            selectRoute(rid);
          }
          return;
        }
        node = node.parentNode;
      }
    });
  }

  const centerBtn = document.getElementById("btn-center");
  if (centerBtn) {
    centerBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      centerOnStop();
    });
  }

  /* ---- Ads / idle overlay ---- */
  const overlay = document.getElementById("idle-overlay");
  const stage = document.getElementById("ad-stage");
  const bar = document.getElementById("ad-bar");
  const idleCountdownEl = document.getElementById("idle-countdown");
  let items = stage ? Array.prototype.slice.call(stage.querySelectorAll(".ad-item")) : [];
  let playlistKey = "";
  let pendingAds = null;
  let idx = -1;
  let started = 0;
  let duration = 10000;
  let advanceTimer = null;
  let adsRunning = false;
  let idleSeconds = Number(cfg.idleSeconds) || 45;
  let idleTimer = null;
  let idleDeadline = 0;
  let countdownRaf = null;

  function hasAds() {
    return items.length > 0;
  }

  function adsKey(ads) {
    return JSON.stringify(
      (ads || []).map(function (a) {
        return [a.url, a.media_type, a.duration_sec];
      })
    );
  }

  function formatCountdown(ms) {
    const sec = Math.max(0, Math.ceil(ms / 1000));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m + ":" + pad2(s);
  }

  function updateCountdownUi() {
    if (!idleCountdownEl) return;
    if (!hasAds() || adsRunning) {
      idleCountdownEl.hidden = true;
      return;
    }
    idleCountdownEl.hidden = false;
    const left = Math.max(0, idleDeadline - Date.now());
    idleCountdownEl.textContent = "Anuncio en " + formatCountdown(left);
    const total = Math.max(10, idleSeconds) * 1000;
    const p = total > 0 ? 1 - left / total : 0;
    idleCountdownEl.style.setProperty("--idle-p", String(Math.min(1, Math.max(0, p))));
  }

  function tickCountdown() {
    updateCountdownUi();
    countdownRaf = requestAnimationFrame(tickCountdown);
  }

  function adoptPlaylist(ads) {
    if (!stage) return;
    stage.innerHTML = "";
    items = [];
    (ads || []).forEach(function (ad) {
      let el;
      if (ad.media_type === "video") {
        el = document.createElement("video");
        el.muted = true;
        el.setAttribute("playsinline", "");
        el.preload = "none";
        el.src = ad.url;
      } else {
        el = document.createElement("img");
        el.alt = "Anuncio";
        el.src = ad.url;
        if (/\.svg(\?|$)/i.test(ad.url || "")) el.classList.add("ad-svg");
      }
      el.classList.add("ad-item");
      el.dataset.duration = String(ad.duration_sec || 10);
      stage.appendChild(el);
      items.push(el);
    });
    updateCountdownUi();
  }

  function showAd(i) {
    const prev = items[idx];
    idx = i;
    const current = items[i];
    items.forEach(function (el) {
      if (el === current) el.classList.add("active");
      else el.classList.remove("active");
    });
    if (prev && prev !== current && prev.tagName === "VIDEO") prev.pause();
    started = performance.now();
    duration = Math.max(3, Number(current.dataset.duration || 10)) * 1000;
    if (current.tagName === "VIDEO") {
      current.loop = items.length === 1;
      current.preload = "auto";
      current.currentTime = 0;
      if (isFinite(current.duration)) duration = Math.max(duration, current.duration * 1000);
      scheduleAdvance(duration + VIDEO_GRACE_MS);
      current.play().catch(function () { scheduleAdvance(FAILED_SKIP_MS); });
    } else if (
      current.tagName === "IMG" &&
      current.complete &&
      current.naturalWidth === 0
    ) {
      scheduleAdvance(FAILED_SKIP_MS);
    } else {
      scheduleAdvance(duration);
    }
    preloadNext();
  }

  function scheduleAdvance(ms) {
    clearTimeout(advanceTimer);
    if (!adsRunning) return;
    advanceTimer = setTimeout(nextAd, Math.max(0, ms));
  }

  function preloadNext() {
    if (items.length < 2) return;
    const upcoming = items[(idx + 1) % items.length];
    if (upcoming.tagName === "VIDEO" && upcoming.preload !== "auto") {
      upcoming.preload = "auto";
      upcoming.load();
    }
  }

  function nextAd() {
    if (!adsRunning) return;
    if (pendingAds) {
      adoptPlaylist(pendingAds);
      pendingAds = null;
      if (!hasAds()) {
        hideIdleOverlay();
        return;
      }
    }
    if (!items.length) return;
    if (items.length === 1 && idx === 0 && items[0].tagName === "VIDEO") {
      scheduleAdvance(duration + VIDEO_GRACE_MS);
      return;
    }
    showAd((idx + 1) % items.length);
  }

  function paintProgress() {
    if (adsRunning && bar) {
      const current = items[idx];
      let p = 0;
      if (
        current &&
        current.tagName === "VIDEO" &&
        isFinite(current.duration) &&
        current.duration > 0
      ) {
        p = current.currentTime / current.duration;
      } else if (current) {
        p = (performance.now() - started) / duration;
      }
      bar.style.width = Math.min(1, Math.max(0, p)) * 100 + "%";
    }
    requestAnimationFrame(paintProgress);
  }

  function updatePlaylist(ads) {
    if (!Array.isArray(ads)) return;
    const key = adsKey(ads);
    if (key === playlistKey) return;
    playlistKey = key;
    if (adsRunning) pendingAds = ads;
    else adoptPlaylist(ads);
  }

  function showIdleOverlay() {
    if (!overlay || !hasAds() || adsRunning) return;
    // Al entrar en anuncio, limpia selección de ruta
    selectedRouteId = null;
    applySelectionStyles();
    overlay.hidden = false;
    adsRunning = true;
    updateCountdownUi();
    showAd(0);
  }

  function hideIdleOverlay(ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    if (!overlay) return;
    adsRunning = false;
    clearTimeout(advanceTimer);
    items.forEach(function (el) {
      if (el.tagName === "VIDEO") el.pause();
      el.classList.remove("active");
    });
    overlay.hidden = true;
    if (bar) bar.style.width = "0%";
    bumpActivity();
    scheduleInvalidate(60);
    scheduleInvalidate(250);
  }

  if (overlay) {
    overlay.addEventListener(
      "pointerdown",
      hideIdleOverlay,
      true
    );
  }

  function bumpActivity() {
    clearTimeout(idleTimer);
    if (!hasAds()) {
      updateCountdownUi();
      return;
    }
    const ms = Math.max(10, idleSeconds) * 1000;
    idleDeadline = Date.now() + ms;
    idleTimer = setTimeout(showIdleOverlay, ms);
    updateCountdownUi();
  }

  ["pointerdown", "touchstart", "keydown"].forEach(function (type) {
    window.addEventListener(
      type,
      function () {
        if (adsRunning) return;
        bumpActivity();
      },
      { passive: true }
    );
  });
  // Scroll en la lista también reinicia el temporizador
  if (routesRoot) {
    routesRoot.addEventListener(
      "scroll",
      function () {
        if (adsRunning) return;
        bumpActivity();
      },
      { passive: true }
    );
  }

  /* ---- Snapshot / SSE ---- */
  function applySnapshot(data) {
    if (typeof data.idle_seconds === "number" && data.idle_seconds >= 10) {
      idleSeconds = data.idle_seconds;
      bumpActivity();
    }
    paintHeader(data);
    updatePlaylist(data.ads);
    const routes = assignDisplayColors(data.routes || []);
    paintRoutesList(routes);
    setStopMarker(data.stop);
    paintRouteLayers(routes);
    if (selectedRouteId) applySelectionStyles();
    scheduleInvalidate(100);
    scheduleInvalidate(400);
  }

  function connectStream() {
    if (!cfg.deviceId || typeof EventSource === "undefined") return;
    const url = ROOT + "/api/paradero/" + encodeURIComponent(cfg.deviceId) + "/stream";
    const es = new EventSource(url, { withCredentials: true });
    es.addEventListener("update", function (ev) {
      try {
        applySnapshot(JSON.parse(ev.data));
      } catch (e) { /* ignore */ }
    });
    es.onerror = function () {
      es.close();
      if (mapStatus) mapStatus.hidden = false;
      setTimeout(connectStream, 3000);
    };
  }

  // Boot
  ensureMap();
  const stopNameEl = document.getElementById("stop-name");
  applySnapshot({
    device: { name: stopNameEl ? stopNameEl.textContent : "" },
    stop: cfg.stop,
    routes: cfg.routes || [],
    ads: cfg.ads || [],
    idle_seconds: idleSeconds,
  });
  if (stage) {
    playlistKey = adsKey(cfg.ads);
    if (!items.length && Array.isArray(cfg.ads) && cfg.ads.length) {
      adoptPlaylist(cfg.ads);
    }
    requestAnimationFrame(paintProgress);
  }
  bumpActivity();
  tickCountdown();
  connectStream();
  window.addEventListener("resize", function () {
    scheduleInvalidate(100);
  });
  window.addEventListener("orientationchange", function () {
    scheduleInvalidate(200);
    scheduleInvalidate(500);
  });
})();

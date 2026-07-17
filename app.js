/* ==========================================================
   ベアウェザー：秋田県 天気＆リアルタイム安全対策
   -- アプリ本体ロジック --
   ========================================================== */

let CSV_DATA = [];
let map = null, markers = null, radarLayer = null, userMarker = null;
let miniMap = null, miniMarker = null;
let pendingLat = null, pendingLng = null;
let nextId = 1;

let currentFilterType = 'all';   // all | 目撃 | 人身被害
let currentPeriod = '1';         // 1 | 3 | 6 | 12 | all （月数）

let js_urls = [];
let js_labels = [];
let isRadarActive = false;
let isBearActive = true;

window.addEventListener('DOMContentLoaded', () => {
  const now = new Date();
  document.getElementById('current-date').textContent = `${now.getFullYear()}年${now.getMonth()+1}月${now.getDate()}日`;

  initMap();
  fetchJmaRadarTimestamps();
  loadOpenMeteoForecast();
  loadServerData();
});

/* ---------------------------------------------------------
   地図初期化
   --------------------------------------------------------- */
function initMap() {
  document.getElementById('loading').style.display = 'none';
  document.getElementById('app').style.display = 'flex';

  const baseTile = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', { attribution: '国土地理院' });
  map = L.map('map', { zoomControl: false, minZoom: 7, maxZoom: 18, layers: [baseTile] }).setView([39.65, 140.1], 8);
  L.control.zoom({ position: 'topright' }).addTo(map);

  markers = L.markerClusterGroup({ spiderfyOnMaxZoom: true, showCoverageOnHover: false });
  map.addLayer(markers);

  const mapUi = document.getElementById('map-ui');
  const floatAlarm = document.getElementById('btn-floating-alarm');

  ['mousedown', 'touchstart', 'dblclick', 'wheel'].forEach(evt => {
    mapUi.addEventListener(evt, e => e.stopPropagation());
    floatAlarm.addEventListener(evt, e => e.stopPropagation());
  });

  initEvents();
}

/* ---------------------------------------------------------
   サーバー(Flask + PostgreSQL)からデータ読み込み / 保存
   --------------------------------------------------------- */
function loadServerData() {
  fetch('data.json') // server.py 側で /api/load へ内部転送される
    .then(res => {
      if (!res.ok) throw new Error("データベースからのデータ取得に失敗しました");
      return res.json();
    })
    .then(bearData => {
      CSV_DATA = Array.isArray(bearData) ? bearData : [];
      const ids = CSV_DATA.map(d => d && d["出没情報ID"] ? parseInt(d["出没情報ID"]) : 0).filter(id => !isNaN(id));
      nextId = ids.length > 0 ? Math.max(...ids) + 1 : 1;
      render();
    })
    .catch(err => {
      console.error("データ同期エラー: ", err);
      document.getElementById('dynamic-alert-text').textContent = "データの同期に失敗しました。再読み込みしてください。";
    });
}

// 🌱 軽量版：新規1件だけをサーバーに送信して追記保存する（全件送信はしない）
function saveEntryToServer(entry, onDone) {
  fetch('/api/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(entry)
  })
  .then(res => res.json())
  .then(resData => {
    if (onDone) onDone(!!resData.success, resData.message || '');
  })
  .catch(err => {
    console.error("サーバー通信失敗:", err);
    if (onDone) onDone(false, 'サーバーに接続できませんでした');
  });
}

/* ---------------------------------------------------------
   天気（Open-Meteo）
   --------------------------------------------------------- */
function loadOpenMeteoForecast() {
  fetch(`https://api.open-meteo.com/v1/forecast?latitude=39.7186&longitude=140.1023&current_weather=true&hourly=temperature_2m,weathercode,precipitation_probability&timezone=Asia%2FTokyo`)
    .then(res => res.json())
    .then(data => {
      if (!data || !data.current_weather) return;
      const cur = data.current_weather;
      document.getElementById('weather-temp').textContent = `${cur.temperature}°C`;

      let wText = "曇り", wIcon = "☁️";
      if (cur.weathercode === 0 || cur.weathercode === 1) { wText = "晴れ"; wIcon = "☀️"; }
      else if (cur.weathercode >= 51 && cur.weathercode <= 67) { wText = "雨"; wIcon = "🌧️"; }
      else if (cur.weathercode >= 71 && cur.weathercode <= 77) { wText = "雪"; wIcon = "❄️"; }
      else if (cur.weathercode >= 95) { wText = "雷雨"; wIcon = "⛈️"; }

      document.getElementById('weather-icon').textContent = wIcon;
      document.getElementById('weather-text').textContent = `秋田市: ${wText}`;

      const timeline = document.getElementById('weather-timeline');
      let tHtml = '';
      for (let i = 0; i < 8; i++) {
        const idx = i * 3;
        const time = data.hourly.time[idx].split('T')[1].substring(0, 5);
        const temp = data.hourly.temperature_2m[idx];
        const pop = data.hourly.precipitation_probability[idx];
        tHtml += `<div class="forecast-item"><div class="forecast-time">${time}</div><div class="forecast-icon">${wIcon}</div><div class="forecast-temp">${temp}°C</div><div class="weather-details-mini"><div class="w-info-mini pop">☔${pop}%</div></div></div>`;
      }
      timeline.innerHTML = tHtml;
    })
    .catch(err => console.error("天気情報の取得に失敗しました:", err));
}

/* ---------------------------------------------------------
   状況サマリー（危険度の判定・表示は行わず、件数と条件だけを
   淡々と示すシンプルな表示にする）
   --------------------------------------------------------- */
const PERIOD_LABELS = { '1': '直近1ヶ月', '3': '直近3ヶ月', '6': '直近6ヶ月', '12': '直近1年間', 'all': '全期間' };
const TYPE_LABELS = { 'all': 'すべての状況', '目撃': '目撃・痕跡', '人身被害': '人身被害' };

function updateStatusSummary(count) {
  const titleEl = document.getElementById('alert-title');
  const descEl = document.getElementById('dynamic-alert-text');
  titleEl.textContent = "🐻 秋田県 クマ目撃情報マップ";
  descEl.textContent = `${PERIOD_LABELS[currentPeriod] || ''}・${TYPE_LABELS[currentFilterType] || ''}を表示中｜該当 ${count} 件`;
}

/* ---------------------------------------------------------
   描画（期間フィルター ＋ 種別フィルター）
   --------------------------------------------------------- */
function parseEntryDate(d) {
  const raw = (d && (d["目撃日時"] || d["発生日時"])) || null;
  if (!raw) return null;
  const t = new Date(String(raw).replace(/-/g, '/')).getTime();
  return isNaN(t) ? null : t;
}

function render() {
  if (!map || !markers) return;

  markers.clearLayers();
  const listEl = document.getElementById('sightings-list');
  listEl.innerHTML = '';

  let count = 0;

  // 最新順にソート
  const sortedData = [...CSV_DATA].sort((a, b) => {
    const timeA = parseEntryDate(a) ?? 0;
    const timeB = parseEntryDate(b) ?? 0;
    return timeB - timeA;
  });

  // 期間の下限（現在時刻からの遡り）を算出
  let cutoffTime = null;
  if (currentPeriod !== 'all') {
    const cutoffDate = new Date();
    cutoffDate.setMonth(cutoffDate.getMonth() - parseInt(currentPeriod, 10));
    cutoffTime = cutoffDate.getTime();
  }

  const fragment = document.createDocumentFragment();

  sortedData.forEach(d => {
    if (!d) return;

    const type = d["情報種別"] || "目撃";
    const dateTime = d["目撃日時"] || d["発生日時"] || "不明";

    // ① 期間フィルター
    if (cutoffTime !== null) {
      const itemTime = parseEntryDate(d);
      if (itemTime === null || itemTime < cutoffTime) return;
    }

    // ② 種別フィルター
    if (currentFilterType !== 'all') {
      if (currentFilterType === '目撃') {
        if (!type.includes('目撃') && !type.includes('痕跡')) return;
      } else if (currentFilterType === '人身被害') {
        if (!type.includes('人身被害')) return;
      }
    }

    count++;

    // 地図ピン
    const latRaw = d["x(緯度)"] || d["緯度"];
    const lngRaw = d["y(経度)"] || d["経度"];
    const lat = parseFloat(latRaw);
    const lng = parseFloat(lngRaw);

    if (!isNaN(lat) && !isNaN(lng)) {
      const col = type.includes('人身') ? '#dc2626' : (type.includes('目撃') ? '#ea580c' : '#64748b');
      const m = L.circleMarker([lat, lng], { radius: 9, fillColor: col, color: '#fff', weight: 2, fillOpacity: 0.8 });
      m.bindPopup(`<b>${escapeHtml(type)}</b><br>${escapeHtml(dateTime)}<br>${escapeHtml(d["地番情報"] || '')}<br>${escapeHtml(d["目撃時の状況"] || '')}`);
      markers.addLayer(m);
    }

    // サイドバーのリスト項目
    const item = document.createElement('div');
    item.className = 'sitem';
    item.innerHTML = `
      <div class="sitem-content">
        <div class="sitem-title">${escapeHtml(d["地番情報"] || '秋田県内')}</div>
        <div class="sitem-meta"><strong>${escapeHtml(dateTime)}</strong> | ${escapeHtml(d["市町村"] || '')}</div>
        <span class="type-tag tag-${escapeHtml(type)}">${escapeHtml(type)}</span>
      </div>
    `;
    if (!isNaN(lat) && !isNaN(lng)) {
      item.addEventListener('click', () => map.setView([lat, lng], 14));
    }
    fragment.appendChild(item);
  });

  if (count === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '選択中の条件に一致する情報はありません。';
    fragment.appendChild(empty);
  }

  listEl.appendChild(fragment);
  document.getElementById('list-count').textContent = `${count} 件の情報`;
  updateStatusSummary(count);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------------------------------------------------------
   JMA 雨雲レーダー
   --------------------------------------------------------- */
async function fetchJmaRadarTimestamps() {
  try {
    const ts = Date.now();
    const targetApis = [
      { prod: "nowc", url: `https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json?_=${ts}` },
      { prod: "nowc", url: `https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json?_=${ts}` },
      { prod: "nowc", url: `https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N3.json?_=${ts}` },
      { prod: "prca", url: `https://www.jma.go.jp/bosai/jmatile/data/prca/targetTimes_N1.json?_=${ts}` },
      { prod: "prca", url: `https://www.jma.go.jp/bosai/jmatile/data/prca/targetTimes_N2.json?_=${ts}` },
      { prod: "prca", url: `https://www.jma.go.jp/bosai/jmatile/data/prca/targetTimes_N3.json?_=${ts}` },
      { prod: "prca", url: `https://www.jma.go.jp/bosai/jmatile/data/prca/targetTimes.json?_=${ts}` }
    ];

    const responses = await Promise.all(targetApis.map(t => fetch(t.url).catch(() => null)));
    let allTimes = {};

    for (let i = 0; i < responses.length; i++) {
      const res = responses[i];
      if (!res || !res.ok) continue;

      const data = await res.json();
      const prod = targetApis[i].prod;

      data.forEach(t => {
        if (t.basetime && t.validtime) {
          if (!allTimes[t.validtime] || t.basetime > allTimes[t.validtime].basetime) {
            t.prod = prod;
            t.element = t.elements ? t.elements[0] : (prod === "nowc" ? "hrpns" : "prca");
            allTimes[t.validtime] = t;
          }
        }
      });
    }

    const sortedTimes = Object.values(allTimes).sort((a, b) => a.validtime.localeCompare(b.validtime));

    js_urls = [];
    js_labels = [];
    const now = new Date();

    sortedTimes.forEach((t) => {
      const year = parseInt(t.validtime.substring(0, 4));
      const month = parseInt(t.validtime.substring(4, 6)) - 1;
      const day = parseInt(t.validtime.substring(6, 8));
      const hour = parseInt(t.validtime.substring(8, 10));
      const min = parseInt(t.validtime.substring(10, 12));

      const validDateUtc = new Date(Date.UTC(year, month, day, hour, min));
      const validDateJst = new Date(validDateUtc.getTime());

      const diffMins = Math.round((validDateJst - now) / 60000);
      if (diffMins < -15) return;

      const timeStr = `${String(validDateJst.getHours()).padStart(2, '0')}:${String(validDateJst.getMinutes()).padStart(2, '0')}`;

      js_urls.push(`https://www.jma.go.jp/bosai/jmatile/data/${t.prod}/${t.basetime}/none/${t.validtime}/surf/${t.element}/{z}/{x}/{y}.png`);

      if (diffMins <= 5 && diffMins >= -15) {
        js_labels.push(`現在 (${timeStr})`);
      } else if (diffMins > 5 && diffMins < 60) {
        js_labels.push(`${diffMins}分後 (${timeStr})`);
      } else {
        const hDiff = Math.floor(diffMins / 60);
        const mDiff = diffMins % 60;
        js_labels.push(mDiff === 0 ? `${hDiff}時間後 (${timeStr})` : `${hDiff}時間${mDiff}分後 (${timeStr})`);
      }
    });

    const slider = document.getElementById('radar-time-slider');
    if (slider && js_urls.length > 0) {
      slider.max = js_urls.length - 1;
      slider.value = 0;
      document.getElementById('time-label').textContent = js_labels[0];
      if (isRadarActive) updateRadarLayer();
    }
  } catch (e) {
    console.error("雨雲データの取得に失敗しました。", e);
    fallbackRadar();
  }
}

function fallbackRadar() {
  const currentTs = Date.now();
  const baseUtc = new Date(Math.floor(currentTs / 300000) * 300000 - 9 * 3600000);
  js_urls = []; js_labels = [];
  for (let i = -2; i <= 6; i++) {
    const targetDt = new Date(baseUtc.getTime() + i * 5 * 60000);
    const y = targetDt.getUTCFullYear(), m = String(targetDt.getUTCMonth() + 1).padStart(2, '0'), d = String(targetDt.getUTCDate()).padStart(2, '0'), h = String(targetDt.getUTCHours()).padStart(2, '0'), min = String(targetDt.getUTCMinutes()).padStart(2, '0');
    const basetime = `${y}${m}${d}${h}${min}00`;
    js_urls.push(`https://www.jma.go.jp/bosai/jmatile/data/nowc/${basetime}/none/${basetime}/surf/hrpns/{z}/{x}/{y}.png`);
    const localTime = new Date(targetDt.getTime() + 9 * 3600000);
    const timeStr = `${String(localTime.getHours()).padStart(2, '0')}:${String(localTime.getMinutes()).padStart(2, '0')}`;
    if (i === 0) js_labels.push(`現在 (${timeStr})`); else if (i < 0) js_labels.push(`${Math.abs(i) * 5}分前 (${timeStr})`); else js_labels.push(`${i * 5}分後 (${timeStr})`);
  }
  const slider = document.getElementById('radar-time-slider');
  slider.max = js_urls.length - 1; slider.value = 2;
  document.getElementById('time-label').textContent = js_labels[2];
  if (isRadarActive) updateRadarLayer();
}

function updateRadarLayer() {
  const slider = document.getElementById('radar-time-slider');
  if (radarLayer) map.removeLayer(radarLayer);
  radarLayer = L.tileLayer(js_urls[slider.value], { opacity: 0.65, zIndex: 1500, maxNativeZoom: 10 }).addTo(map);
}

/* ---------------------------------------------------------
   熊よけ爆竹サイレン
   --------------------------------------------------------- */
async function playBearAlarm(event) {
  if (event && typeof event.stopPropagation === 'function') event.stopPropagation();

  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    const audioCtx = new AudioContextClass();
    if (audioCtx.state === 'suspended') await audioCtx.resume();

    const now = audioCtx.currentTime;
    const duration = 10.0;

    const masterGain = audioCtx.createGain();
    masterGain.gain.setValueAtTime(0.0, now);
    masterGain.gain.linearRampToValueAtTime(1.5, now + 0.1);
    masterGain.gain.setValueAtTime(1.5, now + duration - 0.5);
    masterGain.gain.linearRampToValueAtTime(0.0, now + duration);

    const osc1 = audioCtx.createOscillator(); osc1.type = 'sawtooth';
    const osc2 = audioCtx.createOscillator(); osc2.type = 'triangle';

    for (let t = 0; t < duration; t += 0.05) {
      const lfo = Math.sin(t * (Math.PI * 2 / 1.5));
      const baseFreq = 800 + (lfo * 150);
      osc1.frequency.setValueAtTime(baseFreq, now + t);
      osc2.frequency.setValueAtTime(baseFreq + 4, now + t);
    }

    const bufferSize = audioCtx.sampleRate * duration;
    const buffer = audioCtx.createBuffer(1, bufferSize, audioCtx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < bufferSize; i++) data[i] = Math.random() * 2 - 1;
    const noise = audioCtx.createBufferSource(); noise.buffer = buffer;

    const noiseGain = audioCtx.createGain(); noiseGain.gain.setValueAtTime(0.08, now);
    const osc1Gain = audioCtx.createGain(); osc1Gain.gain.setValueAtTime(0.6, now);
    const osc2Gain = audioCtx.createGain(); osc2Gain.gain.setValueAtTime(0.5, now);

    osc1.connect(osc1Gain); osc1Gain.connect(masterGain);
    osc2.connect(osc2Gain); osc2Gain.connect(masterGain);
    noise.connect(noiseGain); noiseGain.connect(masterGain);
    masterGain.connect(audioCtx.destination);

    osc1.start(now); osc2.start(now); noise.start(now);
    osc1.stop(now + duration); osc2.stop(now + duration); noise.stop(now + duration);
  } catch (e) {
    console.error("Audio Playback Error:", e);
    alert("音声発信が制限されました。もう一度ボタンをしっかりとタップしてください。");
  }
}

/* ---------------------------------------------------------
   投稿モーダル（サーバーDBへの保存）
   --------------------------------------------------------- */
function openPostModal() {
  document.getElementById('modal-overlay').classList.add('open');
  const dateInput = document.getElementById('f-date');
  if (!dateInput.value) {
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    dateInput.value = now.toISOString().slice(0, 16);
  }
  initMiniMap();
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  document.getElementById('f-loc').value = '';
  document.getElementById('f-city').value = '';
  document.getElementById('f-note').value = '';
  if (miniMarker) { miniMarker.remove(); miniMarker = null; }
  pendingLat = null; pendingLng = null;
  document.getElementById('coord-info').textContent = '場所が未選択です';
}

function initMiniMap() {
  setTimeout(() => {
    if (!miniMap) {
      miniMap = L.map('mini-map', { attributionControl: false }).setView([39.7186, 140.1023], 9);
      L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png').addTo(miniMap);
      miniMap.on('click', e => {
        pendingLat = e.latlng.lat; pendingLng = e.latlng.lng;
        document.getElementById('coord-info').textContent = `緯度: ${pendingLat.toFixed(4)} / 経度: ${pendingLng.toFixed(4)}`;
        if (miniMarker) miniMarker.remove();
        miniMarker = L.marker([pendingLat, pendingLng]).addTo(miniMap);
      });
    } else {
      miniMap.invalidateSize();
    }
  }, 150);
}

function submitPost() {
  const city = document.getElementById('f-city').value;
  const loc = document.getElementById('f-loc').value.trim();
  const dateVal = document.getElementById('f-date').value;

  if (!city) { alert('市町村を選択してください'); return; }
  if (!pendingLat || !pendingLng) { alert('地図上をタップして場所を指定してください'); return; }
  if (!dateVal) { alert('目撃日時を入力してください'); return; }

  const entry = {
    "出没情報ID": String(nextId + 1),
    "情報種別": document.getElementById('f-type').value,
    "市町村": city,
    "地番情報": '秋田県' + city + (loc || ''),
    "目撃日時": dateVal.replace('T', ' '),
    "獣種": 'ツキノワグマ',
    "目撃時の状況": document.getElementById('f-note').value.trim() || '（投稿情報）',
    "x(緯度)": String(pendingLat),
    "y(経度)": String(pendingLng)
  };

  const submitBtn = document.getElementById('modal-submit-btn');
  submitBtn.disabled = true;
  submitBtn.textContent = '保存中...';

  // 🌱 新規1件だけをサーバーへ送信（既存データの再送信はしない軽量方式）
  saveEntryToServer(entry, (success, message) => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'サーバーに保存する';
    if (success) {
      nextId++;
      CSV_DATA.unshift(entry);
      closeModal();
      render();
    } else {
      alert('サーバーへの保存に失敗しました: ' + message);
    }
  });
}

/* ---------------------------------------------------------
   イベント登録
   --------------------------------------------------------- */
function initEvents() {
  document.getElementById('btn-alarm').onclick = playBearAlarm;
  document.getElementById('btn-floating-alarm').onclick = playBearAlarm;

  document.getElementById('btn-radar-toggle').onclick = function () {
    isRadarActive = !isRadarActive;
    document.getElementById('radar-slider-box').style.display = isRadarActive ? 'block' : 'none';
    if (isRadarActive) { this.style.background = '#ef4444'; this.textContent = '☀️ 雨雲を消す'; updateRadarLayer(); }
    else { this.style.background = '#2563eb'; this.textContent = '🌧️ 雨雲レーダーを表示'; if (radarLayer) { map.removeLayer(radarLayer); radarLayer = null; } }
  };
  document.getElementById('radar-time-slider').oninput = function () {
    document.getElementById('time-label').textContent = js_labels[this.value];
    if (isRadarActive) updateRadarLayer();
  };
  document.getElementById('btn-bear-toggle').onclick = function () {
    isBearActive = !isBearActive;
    if (isBearActive) { this.style.background = '#1e293b'; this.textContent = '🐻 熊マーカーを隠す'; map.addLayer(markers); }
    else { this.style.background = '#64748b'; this.textContent = '🐻 熊マーカーを表示'; map.removeLayer(markers); }
  };
  document.getElementById('btn-gps').onclick = () => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(pos => {
        const lat = pos.coords.latitude, lng = pos.coords.longitude;
        map.flyTo([lat, lng], 13);
        if (userMarker) map.removeLayer(userMarker);
        userMarker = L.circleMarker([lat, lng], { radius: 8, fillColor: '#2563eb', color: '#fff', weight: 2 }).addTo(map).bindPopup("現在地").openPopup();
      });
    }
  };

  // 📅 期間フィルター
  document.querySelectorAll('#period-filters .fbtn').forEach(btn => {
    btn.onclick = function () {
      document.querySelectorAll('#period-filters .fbtn').forEach(b => b.classList.remove('on'));
      this.classList.add('on');
      currentPeriod = this.getAttribute('data-period');
      render();
    };
  });

  // 🔍 状況種別フィルター
  document.querySelectorAll('#type-filters .fbtn').forEach(btn => {
    btn.onclick = function () {
      document.querySelectorAll('#type-filters .fbtn').forEach(b => b.className = 'fbtn');
      currentFilterType = this.getAttribute('data-t');
      if (currentFilterType === 'all') this.classList.add('on');
      else if (currentFilterType === '目撃') this.classList.add('on-orange');
      else if (currentFilterType === '人身被害') this.classList.add('on-red');
      render();
    };
  });

  // 📝 投稿モーダル
  document.getElementById('btn-open-post').onclick = openPostModal;
  document.getElementById('modal-close-btn').onclick = closeModal;
  document.getElementById('modal-cancel-btn').onclick = closeModal;
  document.getElementById('modal-submit-btn').onclick = submitPost;
  document.getElementById('modal-overlay').addEventListener('click', (e) => {
    if (e.target.id === 'modal-overlay') closeModal();
  });

  // 📱 サイドバー折りたたみ（スマホ用：下部シートの高さを縮める）
  const sidebar = document.getElementById('sidebar');
  const toggleBtn = document.getElementById('sidebar-toggle');
  toggleBtn.onclick = () => {
    sidebar.classList.toggle('collapsed');
    toggleBtn.textContent = sidebar.classList.contains('collapsed') ? '▼ 一覧を表示' : '▲ 地図を優先表示';
    setTimeout(() => { if (map) map.invalidateSize(); }, 260);
  };

  // 🖥️ サイドバー最小化（PC用：端のタブで幅0まで畳んで地図を全画面に）
  const edgeToggleBtn = document.getElementById('sidebar-edge-toggle');
  edgeToggleBtn.onclick = () => {
    sidebar.classList.toggle('mini');
    edgeToggleBtn.textContent = sidebar.classList.contains('mini') ? '›' : '‹';
    edgeToggleBtn.title = sidebar.classList.contains('mini') ? 'サイドバーを展開' : 'サイドバーを最小化';
    setTimeout(() => { if (map) map.invalidateSize(); }, 260);
  };
}

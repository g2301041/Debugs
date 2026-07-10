let CSV_DATA = [];
let map = null;
let markers = null;
let miniMap = null;
let miniMarker = null;
let userGpsMarker = null;
let currentFilterType = 'all';
let currentFilterYear = 'all';
let nextId = 1;
let pendingLat = null;
let pendingLng = null;
let mapInitialized = false;
let currentFilterType = 'all';
let currentPeriod = '1'; // ⭐ 初期表示を「1ヶ月」に設定

window.addEventListener('DOMContentLoaded', () => {
  const now = new Date();
  document.getElementById('current-date').textContent = `${now.getFullYear()}年${now.getMonth()+1}月${now.getDate()}日`;
  if (typeof logDebug === 'function') logDebug("初期化開始: data.json を読み込み中...");

  fetch('data.json')
    .then(res => {
      if(!res.ok) throw new Error("data.jsonが見つかりません。新規作成します。");
      return res.json();
    })
    .then(bearData => {
      CSV_DATA = Array.isArray(bearData) ? bearData : [];
      if (typeof logDebug === 'function') logDebug(`data.json 読み込み成功: ${CSV_DATA.length} 件`);
      initSystem();
    })
    .catch(err => {
      if (typeof logDebug === 'function') logDebug(`警告: ${err.message}`);
      CSV_DATA = [];
      initSystem();
    });
});

function initSystem() {
  const ids = CSV_DATA.map(d => d && d["出没情報ID"] ? parseInt(d["出没情報ID"]) : 0).filter(id => !isNaN(id));
  if (ids.length > 0) nextId = Math.max(...ids) + 1;

  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (pos) => finishSetup({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      () => finishSetup(null),
      { enableHighAccuracy: true, timeout: 3500 }
    );
  } else {
    finishSetup(null);
  }
}

function finishSetup(gpsCoords) {
  document.getElementById('loading').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  initMapContainer(gpsCoords);
  initEvents();
  if (typeof initDebugEvents === 'function') initDebugEvents();
  setupYearSelect();
  render();
  if (typeof updateDebugJsonView === 'function') updateDebugJsonView();
}

// 🌟 データをPythonサーバー経由で data.json に保存する関数
function saveToServer() {
  fetch('/api/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(CSV_DATA)
  })
  .then(res => res.json())
  .then(resData => {
    if(resData.success) {
      if (typeof logDebug === 'function') logDebug("サーバー上の data.json がリアルタイム自動更新されました。");
    } else {
      if (typeof logDebug === 'function') logDebug("サーバー保存エラー: " + resData.message);
    }
  })
  .catch(err => {
    if (typeof logDebug === 'function') logDebug("サーバー通信失敗 (server.py が起動しているか確認してください): " + err.message);
  });
}

function initMapContainer(gpsCoords) {
  if (mapInitialized) return;
  const baseTile = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', { attribution: '国土地理院' });
  let startLat = 39.7200, startLng = 140.1000, startZoom = 9;
  if (gpsCoords) { startLat = gpsCoords.lat; startLng = gpsCoords.lng; startZoom = 14; }

  map = L.map('map', { zoomControl: false, minZoom: 7, maxZoom: 18, layers: [baseTile] }).setView([startLat, startLng], startZoom);
  map.on('moveend', () => { const center = map.getCenter(); loadWeatherData(center.lat, center.lng, "表示エリア周辺"); });

  if (gpsCoords) {
    userGpsMarker = L.circleMarker([startLat, startLng], { radius: 8, fillColor: '#3498db', color: '#fff' }).addTo(map).bindPopup("現在地");
    loadWeatherData(startLat, startLng, "現在地周辺");
  } else {
    loadWeatherData(39.7200, 140.1000, "秋田市周辺");
  }

  L.control.zoom({ position: 'topleft' }).addTo(map);
  markers = L.markerClusterGroup({ spiderfyOnMaxZoom: true, showCoverageOnHover: false });
  map.addLayer(markers);
  mapInitialized = true;
}

function parseWeatherCode(code) {
  if (code === 0 || code === 1) return { text: "晴れ", icon: "☀️" };
  if (code >= 2 && code <= 3) return { text: "曇り", icon: "☁️" };
  if (code >= 51 && code <= 67) return { text: "雨", icon: "🌧️" };
  if (code >= 71 && code <= 77) return { text: "雪", icon: "❄️" };
  return { text: "曇り", icon: "☁️" };
}

function loadWeatherData(lat, lng, areaLabel) {
  const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lng}&current_weather=true&hourly=temperature_2m,weathercode,precipitation_probability,precipitation,wind_speed_10m&timezone=Asia%2FTokyo`;
  fetch(url).then(res => res.json()).then(data => {
    if (data && data.current_weather) {
      const cur = data.current_weather;
      const info = parseWeatherCode(cur.weathercode);
      document.getElementById('weather-temp').textContent = `${cur.temperature}°C`;
      document.getElementById('weather-icon').textContent = info.icon;
      document.getElementById('weather-text').textContent = `${areaLabel}: ${info.text}`;
    }
    updateForecastTimeline(data);
  }).catch(() => {});
}

function updateForecastTimeline(data) {
  const timelineEl = document.getElementById('weather-timeline');
  if (!data || !data.hourly) return;
  const hourly = data.hourly;
  const fragment = document.createDocumentFragment();
  for (let k = 0; k < 6; k++) {
    const idx = k * 3;
    const card = document.createElement('div');
    card.className = 'forecast-item';
    card.innerHTML = `
      <div class="forecast-time">${hourly.time[idx].split('T')[1].substring(0,5)}</div>
      <div class="forecast-icon">${parseWeatherCode(hourly.weathercode[idx]).icon}</div>
      <div class="forecast-temp">${hourly.temperature_2m[idx]}°C</div>
    `;
    fragment.appendChild(card);
  }
  timelineEl.innerHTML = ''; timelineEl.appendChild(fragment);
}

function setupYearSelect() {
  const years = new Set();
  CSV_DATA.forEach(d => { if (d && d["目撃日時"]) { const y = d["目撃日時"].substring(0,4); if(y.length===4) years.add(y); } });
  const select = document.getElementById('year-select');
  select.innerHTML = '<option value="all">すべての年</option>';
  Array.from(years).sort((a,b)=>b-a).forEach(y => {
    const opt = document.createElement('option'); opt.value = y; opt.textContent = y + '年'; select.appendChild(opt);
  });
}

function initEvents() {
  // （既存の爆竹やGPS、雨雲レーダーの処理はそのまま残す）
  document.getElementById('btn-alarm').onclick = playBearAlarm;
  document.getElementById('btn-floating-alarm').onclick = playBearAlarm;
  // ...

  // ------------------------------------------
  // 📅 【新設】期間フィルターボタンのクリックイベント
  // ------------------------------------------
  document.querySelectorAll('.fbtn[data-period]').forEach(btn => {
    btn.onclick = function() {
      // 期間用のボタンだけ、一度選択クラス「on」をすべて外す
      document.querySelectorAll('.fbtn[data-period]').forEach(b => b.classList.remove('on'));
      // クリックされたボタンに「on」をつける
      this.classList.add('on');
      
      // 選択された期間（1, 3, 6, 12, all）を変数に代入して再描画
      currentPeriod = this.getAttribute('data-period');
      
      render();
      updateAlertBanner();
    };
  });

  // ------------------------------------------
  // 🔍 【最適化】状況種別ボタンのクリックイベント
  // ------------------------------------------
  document.querySelectorAll('.fbtn[data-t]').forEach(btn => { 
    btn.onclick = function() { 
      // 種別用のボタンだけ、一度クラスをリセット
      document.querySelectorAll('.fbtn[data-t]').forEach(b => b.className = 'fbtn'); 
      
      currentFilterType = this.getAttribute('data-t'); 
      
      if (currentFilterType === 'all') this.classList.add('on');
      else if (currentFilterType === '目撃') this.classList.add('on-orange');
      else if (currentFilterType === '人身被害') this.classList.add('on-red');
      
      render(); 
      updateAlertBanner(); 
    }; 
  });
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  document.getElementById('f-loc').value = ''; document.getElementById('f-city').value = ''; document.getElementById('f-note').value = '';
  if (miniMarker) { miniMarker.remove(); miniMarker = null; }
  pendingLat = null; pendingLng = null;
}

function initMiniMap() {
  setTimeout(() => {
    if (!miniMap) {
      miniMap = L.map('mini-map', { attributionControl: false }).setView([39.7200, 140.1000], 9);
      L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png').addTo(miniMap);
      miniMap.on('click', e => {
        pendingLat = e.latlng.lat; pendingLng = e.latlng.lng;
        document.getElementById('coord-info').textContent = `緯度: ${pendingLat.toFixed(4)} / 経度: ${pendingLng.toFixed(4)}`;
        if (miniMarker) miniMarker.remove();
        miniMarker = L.marker([pendingLat, pendingLng]).addTo(miniMap);
      });
    } else { miniMap.invalidateSize(); }
  }, 150);
}

function submitPost() {
  const loc = document.getElementById('f-loc').value.trim();
  const city = document.getElementById('f-city').value;
  if (!loc || !city || !pendingLat) { alert('地図上で場所を指定してください'); return; }
  
  const entry = {
    "出没情報ID": String(nextId++),
    "情報種別": document.getElementById('f-type').value,
    "市町村": city, 
    "地番情報": '秋田県' + city + loc,
    "目撃日時": document.getElementById('f-date').value.replace('T', ' ').substring(0,16),
    "獣種": 'ツキノワグマ', "目撃時の状況": document.getElementById('f-note').value.trim() || '（手動投稿情報）',
    "x(緯度)": String(pendingLat), "y(経度)": String(pendingLng)
  };

  CSV_DATA.unshift(entry);
  if (typeof logDebug === 'function') logDebug(`投稿成功 (ID: ${entry.出微情報ID || entry.出没情報ID})`);
  
  saveToServer(); // 🌟 投稿完了と同時に自動でサーバーへ保存リクエストを出す
  
  closeModal(); setupYearSelect(); render(); if (typeof updateDebugJsonView === 'function') updateDebugJsonView();
}

function render() {
  if (!map || !markers) return;

  markers.clearLayers();
  const listEl = document.getElementById('sightings-list');
  listEl.innerHTML = '';
  
  let count = 0;

  // 最新順（日付の新しい順）にソート
  const sortedData = [...CSV_DATA].sort((a, b) => {
    const dateAStr = a["目撃日時"] || a["発生日時"] || "1970/01/01 00:00";
    const dateBStr = b["目撃日時"] || b["発生日時"] || "1970/01/01 00:00";
    const timeA = new Date(dateAStr.replace(/-/g, '/')).getTime();
    const timeB = new Date(dateBStr.replace(/-/g, '/')).getTime();
    return timeB - timeA;
  });

  // 期間切り替えの計算用タイムスタンプを取得
  const now = new Date();
  let cutoffTime = null;

  if (currentPeriod !== 'all') {
    const monthsToSubtract = parseInt(currentPeriod);
    const cutoffDate = new Date();
    cutoffDate.setMonth(now.getMonth() - monthsToSubtract);
    cutoffTime = cutoffDate.getTime();
  }

  // ループ処理と絞り込み
  sortedData.forEach(d => {
    if (!d) return;
    
    const type = d["情報種別"] || "目撃";
    const dateTime = d["目撃日時"] || d["発生日時"] || "不明";
    
    // ① 期間での絞り込み
    if (cutoffTime !== null) {
      const itemTime = new Date(dateTime.replace(/-/g, '/')).getTime();
      if (isNaN(itemTime) || itemTime < cutoffTime) return; // 基準より古ければ非表示
    }

    // ② 種別での絞り込み
    if (currentFilterType !== 'all') {
      if (currentFilterType === '目撃') {
        if (!type.includes('目撃') && !type.includes('痕跡')) return;
      } else if (currentFilterType === '人身被害') {
        if (!type.includes('人身被害')) return;
      }
    }
    
    count++;

    // （以降、マップピンの生成やリストへの append 処理は既存のままでOKです）
    const latRaw = d["x(緯度)"] || d["緯度"];
    const lngRaw = d["y(経度)"] || d["経度"];
    const lat = parseFloat(latRaw); 
    const lng = parseFloat(lngRaw);

    if (!isNaN(lat) && !isNaN(lng)) {
      const col = type.includes('人身') ? '#dc2626' : (type.includes('目撃') ? '#ea580c' : '#64748b');
      const m = L.circleMarker([lat, lng], { radius: 9, fillColor: col, color: '#fff', weight: 2, fillOpacity: 0.8 });
      m.bindPopup(`<b>${type}</b><br>${dateTime}<br>${d["地番情報"] || ''}`);
      markers.addLayer(m);
    }

    const item = document.createElement('div');
    item.className = 'sitem';
    item.innerHTML = `
      <div class="sitem-content" onclick="if(!isNaN(${lat}) && !isNaN(${lng})) map.setView([${lat},${lng}], 14)">
        <div class="sitem-title">${d["地番情報"] || '秋田県内'}</div>
        <div class="sitem-meta"><strong>${dateTime}</strong> | ${d["市町村"] || ''}</div>
        <span class="type-tag tag-${type}">${type}</span>
      </div>
    `;
    listEl.appendChild(item);
  });

  document.getElementById('list-count').textContent = `${count} 件の情報`;
}

  document.getElementById('list-count').textContent = `該当データ: ${filtered.length}件`;

  filtered.forEach(d => {
    if (!d || !d["x(緯度)"] || !d["y(経度)"]) return;
    const latNum = parseFloat(d["x(緯度)"]); const lngNum = parseFloat(d["y(経度)"]);
    if (isNaN(latNum) || isNaN(lngNum)) return;

    const marker = L.marker([latNum, lngNum]);
    marker.bindPopup(`<b>${d["情報種別"]}</b><br>${d["地番情報"]}<br>${d["目撃日時"]}<br>${d["目撃時の状況"]}`);
    markers.addLayer(marker);

    const item = document.createElement('div');
    item.className = 'sitem';
    item.innerHTML = `
      <div class="sitem-title">${d["市町村"]} (${d["目撃日時"] ? d["目撃日時"].split(' ')[0] : ''})</div>
      <div class="sitem-meta">${d["目撃時の状況"] || ''}</div>
      <span class="type-tag tag-${d["情報種別"] ? d["情報種別"].split('(')[0] : '目撃'}">${d["情報種別"]}</span>
    `;
    item.onclick = () => { map.setView([latNum, lngNum], 14); marker.openPopup(); };
    fragment.appendChild(item);
  });
  listEl.innerHTML = ''; listEl.appendChild(fragment);
}

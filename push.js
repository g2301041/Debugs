(() => {
  'use strict';
  const STORE = 'bear-web-push-v1';
  let state;
  try { state = JSON.parse(localStorage.getItem(STORE)); } catch (_) {}
  if (!state) state = { last: null, fixed: null, lastAt: '', enabled: false };
  if (!state.token) {
    state.token = Array.from(crypto.getRandomValues(new Uint8Array(32)),
      b => b.toString(16).padStart(2, '0')).join('');
  }
  const persist = () => localStorage.setItem(STORE, JSON.stringify(state));
  let registration, config, dialog, message, description;
  const show = text => { message.textContent = text; };
  async function api(path, method = 'GET', data) {
    const response = await fetch('/api/push/' + path, {
      method, headers: { 'Content-Type': 'application/json',
        Authorization: 'Bearer ' + state.token },
      body: data === undefined ? undefined : JSON.stringify(data)
    });
    let result;
    try { result = await response.json(); } catch (_) { throw Error('サーバーの応答を確認できません'); }
    if (!response.ok) throw Error(result.error || '通信に失敗しました');
    return result;
  }
  function describe() {
    const point = p => p ? `${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}` : '未設定';
    description.textContent = `通知：${state.enabled ? '有効' : '停止中'}\n`
      + `最終位置：${point(state.last)}\n`
      + (state.lastAt ? `取得日時：${new Date(state.lastAt).toLocaleString()}\n` : '')
      + `指定場所：${point(state.fixed)}\nどちらかから5km以内の新しい情報を通知します。`;
  }
  async function sync() {
    if (!state.enabled) return;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) throw Error('通知登録が切れています。「通知を有効にする」を押してください');
    await api('device', 'POST', { ...state, subscription: subscription.toJSON() });
  }
  async function setLast(position) {
    state.last = { lat: position.coords.latitude, lng: position.coords.longitude };
    state.lastAt = new Date(position.timestamp || Date.now()).toISOString();
    persist(); describe();
    try { await sync(); show('最終位置を保存しました。'); }
    catch (error) { show('端末には保存済みですが、サーバーへの保存に失敗：' + error.message); }
  }
  function button(label, handler) {
    const el = document.createElement('button');
    el.type = 'button'; el.textContent = label;
    el.style.cssText = 'padding:10px;margin:4px;border:1px solid #bbb;border-radius:6px;cursor:pointer';
    el.onclick = async () => {
      el.disabled = true;
      try { await handler(); } catch (error) { show(error.message); }
      finally { el.disabled = false; describe(); }
    };
    dialog.append(el);
  }
  async function start() {
    const open = document.createElement('button');
    open.type = 'button'; open.textContent = '🔔 プッシュ通知の設定';
    open.style.cssText = 'padding:10px;margin-top:6px;width:100%';
    (document.querySelector('.action-panel') || document.body).append(open);
    dialog = document.createElement('dialog');
    dialog.style.cssText = 'margin:auto;padding:20px;max-width:420px;width:90%;border:1px solid #aaa;border-radius:12px;max-height:85vh;overflow:auto';
    dialog.innerHTML = '<h3>熊情報のプッシュ通知</h3>';
    description = document.createElement('p'); description.style.whiteSpace = 'pre-line';
    message = document.createElement('p'); message.setAttribute('role', 'status');
    message.style.whiteSpace = 'pre-line';
    dialog.append(description, message); document.body.append(dialog);
    open.onclick = () => { describe(); dialog.showModal(); };
    describe();
    if (!window.isSecureContext || !('serviceWorker' in navigator)
        || !('PushManager' in window) || !('Notification' in window)) {
      show('この環境ではWebプッシュを利用できません。HTTPSで開いてください。iPhoneはホーム画面に追加し、そのアイコンから開いてください。');
      button('閉じる', () => dialog.close()); return;
    }
    button('現在の位置を取得する', () => {
      show('位置情報を取得中…');
      if (!navigator.geolocation) throw Error('位置情報に対応していません');
      navigator.geolocation.getCurrentPosition(setLast,
        error => show(({1:'位置情報の許可が必要です。スマホとブラウザーの位置情報設定を確認してください。',
                       2:'位置を取得できませんでした。',3:'時間切れです。もう一度試してください。'})[error.code] || error.message),
        { enableHighAccuracy: false, timeout: 15000, maximumAge: 0 });
    });
    button('地図で指定場所を選ぶ', () => {
      if (typeof map === 'undefined' || !map) throw Error('地図を読み込み中です');
      dialog.close();
      const hint = document.createElement('div');
      hint.textContent = '通知の基準にする場所を地図上でタップしてください（キャンセル）';
      hint.style.cssText = 'position:fixed;top:10px;left:5%;width:90%;padding:14px;background:#fff3cd;z-index:99999;cursor:pointer';
      document.body.append(hint);
      const pick = async event => {
        map.off('click', pick); hint.remove();
        state.fixed = { lat: event.latlng.lat, lng: event.latlng.lng };
        persist(); describe(); dialog.showModal();
        try { await sync(); show('指定場所を保存しました。'); }
        catch (error) { show('サーバーへの保存に失敗：' + error.message); }
      };
      hint.onclick = () => { map.off('click', pick); hint.remove(); dialog.showModal(); };
      map.on('click', pick);
    });
    for (const [key, label] of [['last', '最終位置'], ['fixed', '指定場所']]) {
      button(label + 'を解除', async () => {
        if (state.enabled && !state[key === 'last' ? 'fixed' : 'last'])
          throw Error('先に通知を停止するか、もう一方の場所を設定してください');
        state[key] = null; if (key === 'last') state.lastAt = '';
        persist(); await sync(); show(label + 'を解除しました。');
      });
    }
    button('通知を有効にする', async () => {
      if (!state.last && !state.fixed) throw Error('先に最終位置か指定場所を設定してください');
      if (!registration || !config?.ready) throw Error('サーバーの通知設定が未完了です。管理者に確認してください');
      // iPhoneでも許可画面が出るよう、クリック直後に呼ぶ。
      if (await Notification.requestPermission() !== 'granted') throw Error('通知が許可されていません');
      let sub = await registration.pushManager.getSubscription();
      if (!sub) {
        const raw = config.publicKey.replace(/-/g, '+').replace(/_/g, '/');
        const bytes = Uint8Array.from(atob(raw + '='.repeat((4 - raw.length % 4) % 4)), c => c.charCodeAt(0));
        sub = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytes });
      }
      await api('device', 'POST', { ...state, subscription: sub.toJSON() });
      state.enabled = true; persist(); show('通知を有効にしました。テスト通知で確認してください。');
    });
    button('テスト通知を送る', async () => {
      await api('test', 'POST', {}); show('テストを送信待ちに登録しました。数秒後に「送信状況」を確認できます。');
    });
    button('送信状況を確認', async () => {
      const result = await api('status');
      show(({none:'まだ通知対象の情報がありません。',pending:'送信待ちです。長く続く場合は通知ワーカーを確認してください。',
        accepted:'通知サービスが受け付けました。端末での表示は端末の設定・通信状態によります。',
        failed:'送信に失敗しました。',cancelled:'通知を取り消しました。',expired:'送信待ちのまま1時間が経過しました。'})[result.state]
        + (result.error ? '\n' + result.error : ''));
    });
    button('通知を停止する', async () => {
      await api('device', 'DELETE'); state.enabled = false; persist();
      show('通知を停止しました。');
    });
    button('閉じる', () => dialog.close());
    window.addEventListener('bear-gps-position', event => setLast(event.detail));
    try {
      registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      registration = await navigator.serviceWorker.ready;
      config = await api('config');
      if (!config.ready) throw Error('サーバーのVAPID設定が未完了です');
      await sync();
    } catch (error) { show(error.message); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();

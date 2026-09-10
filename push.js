(() => {
  'use strict';
  const STORE = 'bear-web-push-v1';
  let state;
  try { state = JSON.parse(localStorage.getItem(STORE)); } catch (_) {}
  if (!state || typeof state !== 'object' || Array.isArray(state)) state = { last: null, fixed: null, lastAt: '', enabled: false };
  if (!state.token) {
    state.token = Array.from(crypto.getRandomValues(new Uint8Array(32)),
      b => b.toString(16).padStart(2, '0')).join('');
  }
  const persist = () => localStorage.setItem(STORE, JSON.stringify(state));
  let registration, config, dialog, message, description;
  const show = text => {
    if (message) message.textContent = text;
    const status = document.getElementById('push-load-status');
    if (status) status.textContent = text;
  };
  function limited(promise, text, ms = 20000) {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error(text)), ms);
    })]).finally(() => clearTimeout(timer));
  }
  async function api(path, method = 'GET', data) {
    const response = await limited(fetch('/api/push/' + path, {
      method, headers: { 'Content-Type': 'application/json',
        Authorization: 'Bearer ' + state.token },
      body: data === undefined ? undefined : JSON.stringify(data)
    }), 'サーバーの応答がありません。少し待ってページを開き直してください。', path === 'retry' ? 110000 : 20000);
    let result;
    try { result = await limited(response.json(), '応答の読み込みが時間切れです'); } catch (_) { throw Error('サーバーの応答を確認できません。HTTP ' + response.status); }
    if (!response.ok) throw Error(result.error || ('通信に失敗しました。HTTP ' + response.status));
    return result;
  }
  function describe() {
    const point = p => p ? `${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}` : '未設定';
    const permission = 'Notification' in window ? Notification.permission : 'unsupported';
    const permissionText = {granted:'許可済み', denied:'ブロック中', default:'未許可', unsupported:'この開き方では未対応'};
    description.textContent = `① ブラウザーの通知許可：${permissionText[permission] || '確認中'}\n`
      + `② サイトへの通知先登録：${state.enabled ? '完了' : '未完了（下の「通知を有効にする」を押してください）'}\n`
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
    show('通知機能を読み込み中…');
    describe();
    if (!window.isSecureContext || !('serviceWorker' in navigator)
        || !('PushManager' in window) || !('Notification' in window)) {
      const apple = /iPhone|iPad|iPod/.test(navigator.userAgent || '')
        || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      if (!window.isSecureContext) {
        show('通知にはHTTPSが必要です。https://test-m7ms.onrender.com/ から開いてください。');
      } else if (apple) {
        show('iPhone・iPadでページを閉じた後も通知を受けるには、ホーム画面への追加が必要です。\n'
          + 'App Storeからのインストールは不要です。\n'
          + '① Safariでこのサイトを開く\n② 共有ボタン →「ホーム画面に追加」→「追加」\n'
          + '③ 追加したアイコンから開き、「通知を有効にする」を押す\n'
          + 'iOS/iPadOS 16.4以降が必要です。すでに追加済みなら、そのアイコンから開いてください。');
      } else {
        show('このブラウザーではWebプッシュを利用できません。Androidでは最新のChromeで直接開いてください。PCでは通常のChromeまたはEdgeで確認してください。');
      }
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
      show('通知の設定を確認しています…');
      if (!state.last && !state.fixed) throw Error('先に最終位置か指定場所を設定してください');
      if (!registration || !config?.ready) throw Error('通知機能の準備が完了していません。画面に出ているエラーを確認してページを開き直してください。');
      if (Notification.permission === 'denied') throw Error('通知はブロックされています。端末またはブラウザーのこのサイトの通知設定で許可してから、開き直してください。');
      // iPhoneでも許可画面が出るよう、クリック直後に呼ぶ。
      show('通知の許可を確認中です。端末の許可画面を確認してください。');
      const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
      if (permission !== 'granted') throw Error('通知はまだ許可されていません。許可画面を閉じた場合は、もう一度お試しください。');
      show('通知は許可されています。通知先を登録しています…');
      let sub = await limited(registration.pushManager.getSubscription(), '通知先の取得が時間切れです。ページを開き直してください。');
      if (!sub) {
        const raw = config.publicKey.replace(/-/g, '+').replace(/_/g, '/');
        const bytes = Uint8Array.from(atob(raw + '='.repeat((4 - raw.length % 4) % 4)), c => c.charCodeAt(0));
        sub = await limited(registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytes }),
          '通知先の登録が時間切れです。通信状態を確認して、もう一度お試しください。');
      }
      await api('device', 'POST', { ...state, subscription: sub.toJSON() });
      state.enabled = true; persist(); show('通知を有効にしました。テスト通知で確認してください。');
    });
    button('テスト通知を送る', async () => {
      if (!state.enabled) throw Error('ブラウザーで許可しただけでは登録は完了しません。先に、この画面の「通知を有効にする」を押してください。');
      show('テスト通知を送信中…');
      const result = await api('test', 'POST', {});
      showDelivery(result.delivery);
    });
    button('送信待ちを再送する', async () => {
      show('この端末の送信待ち通知を再送中…');
      const result = await api('retry', 'POST', {});
      showDelivery(result.delivery);
    });
    button('送信状況を確認', async () => {
      const result = await api('status');
      show(({none:'まだ通知対象の情報がありません。',pending:'送信待ちです。少し待って「送信待ちを再送する」を押してください。',
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
    window.addEventListener('bear-post-notification', event => {
      const messages = {
        accepted: '投稿を保存し、通知サービスへの送信を完了しました。',
        none: '投稿を保存しました。5km以内に通知対象の登録端末がありません。',
        pending: '投稿は保存済みです。通知の送信待ちが残っています。',
        failed: '投稿は保存済みですが、通知の送信に失敗しました。',
        not_configured: '投稿は保存済みですが、通知用の鍵を準備できませんでした。'
      };
      show(messages[event.detail] || '投稿は保存済みです。通知サーバーが無料版に更新されているか確認してください。');
    });
    try {
      config = await api('config');
      if (!['20260910-free1', '20260910-free2'].includes(config.version)) throw Error('サーバーが無料版に更新されていません。server.py・web_push.pyのデプロイを確認してください。');
      if (!config.ready) throw Error(config.error || '通知用の鍵を準備できませんでした。');
      registration = await limited(navigator.serviceWorker.register('/sw.js', { scope: '/' }), '通知機能を読み込めません。sw.jsの配置を確認してください。');
      registration = await limited(navigator.serviceWorker.ready, '通知機能の起動が時間切れです。sw.jsの配置を確認してページを開き直してください。');
      await sync();
      show(Notification.permission === 'denied' ? '端末側で通知がブロックされています。通知設定を確認してください。' :
        state.enabled ? '通知先の登録を確認しました。「テスト通知を送る」で確認してください。' :
        '通知機能の読み込みが完了しました。場所を設定して、この画面の「通知を有効にする」を押してください。');
    } catch (error) { show(error.message); }
  }
  function showDelivery(state) {
    show(({accepted:'通知サービスが受け付けました。端末の通知欄を確認してください。',
      pending:'送信待ちが残っています。少し待って「送信待ちを再送する」を押してください。',
      failed:'通知の送信に失敗しました。「送信状況を確認」でエラーを確認してください。',
      not_configured:'通知用の鍵を準備できませんでした。ページを開き直して設定状態を確認してください。',
      none:'送信対象の通知はありません。',cancelled:'通知は停止されています。',expired:'通知の送信期限が過ぎています。'})[state]
      || '「送信状況を確認」で結果を確認してください。');
  }
  const run = () => start().catch(error => show('通知機能を開始できません：' + error.message));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();

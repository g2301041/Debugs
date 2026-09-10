(() => {
  // 専用URLのキーを読み取ります。
  const params = new URLSearchParams(
    location.hash.slice(1)
  );

  const supplied = params.get("line");

  if (supplied) {
    sessionStorage.setItem(
      "bear-line-key",
      supplied
    );

    // アドレス欄から専用キーを取り除きます。
    history.replaceState(
      null,
      "",
      location.pathname + location.search
    );
  }

  const key = sessionStorage.getItem(
    "bear-line-key"
  );

  // 普通のURLで開いた人は、元のサイトをそのまま使えます。
  if (!key) {
    return;
  }

  const box = document.createElement("div");

  box.style.cssText = [
    "padding:8px",
    "background:white",
    "color:#222",
    "font-size:12px"
  ].join(";");

  const status = document.createElement("p");

  status.setAttribute(
    "role",
    "status"
  );

  status.textContent =
    "📍で取得する現在地をLINE通知の判定用に保存します。" +
    "通知を使う場合は📍を押してください。";

  const stop = document.createElement("button");

  stop.type = "button";

  stop.textContent =
    "LINE通知・位置共有を停止";

  box.append(status, stop);

  const host =
    document.querySelector(".action-panel") ||
    document.body;

  host.appendChild(box);

  if (!navigator.geolocation) {
    status.textContent =
      "この端末では位置情報を取得できません";

    return;
  }

  const original =
    navigator.geolocation.getCurrentPosition.bind(
      navigator.geolocation
    );

  let enabled = true;
  let registered = false;
  let tracking = false;
  let busy = false;
  let chain = Promise.resolve();
  let lastSent = 0;

  // =====================================================
  // 位置をサーバーに送る
  // =====================================================

  async function post(body) {
    const controller = new AbortController();

    const timeout = setTimeout(() => {
      controller.abort();
    }, 20000);

    try {
      const res = await fetch("/line/location", {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "X-Line-Location-Key": key
        },
        body: JSON.stringify(body)
      });

      const data = await res.json();

      if (!res.ok || !data.success) {
        throw new Error(
          data.message ||
          "位置の保存に失敗しました"
        );
      }

      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  // =====================================================
  // 元のGPS処理から取得した位置を保存
  // =====================================================

  function capture(position) {
    if (
      !enabled ||
      busy ||
      Date.now() - lastSent < 50000
    ) {
      return;
    }

    // 古いキャッシュ位置を現在地として登録しません。
    if (
      Date.now() - position.timestamp > 60000
    ) {
      return;
    }

    tracking = true;
    busy = true;

    chain = chain
      .then(async () => {
        if (!enabled) {
          return;
        }

        const data = await post({
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          accuracy: position.coords.accuracy,
          enable: !registered
        });

        registered = true;
        lastSent = Date.now();

        status.textContent =
          "LINE通知用の位置を更新しました（" +
          new Date().toLocaleTimeString() +
          "）。最後の位置は" +
          data.ttl +
          "分間有効です。";
      })
      .catch(error => {
        status.textContent = error.message;
      })
      .finally(() => {
        busy = false;
      });
  }

  // =====================================================
  // 元のapp.jsのGPS結果をそのまま受け取ります。
  // 元の地図表示用コールバックも必ず呼びます。
  // =====================================================

  try {
    const wrapped = function (
      success,
      failure,
      options
    ) {
      return original(
        position => {
          try {
            success(position);
          } finally {
            capture(position);
          }
        },
        failure,
        options
      );
    };

    navigator.geolocation.getCurrentPosition =
      wrapped;

    if (
      navigator.geolocation.getCurrentPosition !==
      wrapped
    ) {
      throw new Error("連携できません");
    }
  } catch (_) {
    status.textContent =
      "このブラウザーでは位置取得を連携できません";

    return;
  }

  // =====================================================
  // GPSを一度使った後は、
  // サイト表示中に約1分ごとに更新します。
  // =====================================================

  const timer = setInterval(() => {
    if (
      !enabled ||
      !tracking ||
      document.hidden ||
      busy
    ) {
      return;
    }

    original(
      capture,
      () => {
        status.textContent =
          "位置を更新できません。" +
          "最後の位置は有効期限まで使われます。";
      },
      {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 0
      }
    );
  }, 60000);

  // =====================================================
  // 通知と位置共有の停止
  // =====================================================

  stop.onclick = async () => {
    enabled = false;

    clearInterval(timer);

    stop.disabled = true;

    // 進行中の位置送信が終わってから停止します。
    await chain;

    try {
      await post({
        stop: true
      });

      sessionStorage.removeItem(
        "bear-line-key"
      );

      status.textContent =
        "通知と位置共有を停止しました。" +
        "再開する場合は専用URLを開き直してください。";
    } catch (error) {
      status.textContent =
        "停止を保存できません：" +
        error.message +
        "。もう一度停止してください。";

      stop.disabled = false;
    }
  };
})();

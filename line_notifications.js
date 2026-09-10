(() => {
  const originalFetch = window.fetch.bind(window);
  const pendingPosts = new Map();

  // 元の投稿内容を変更せず、通知の重複防止IDだけ追加します。
  window.fetch = async (input, options) => {
    const url = typeof input === "string"
      ? new URL(input, location.href)
      : null;

    const isPost =
      url &&
      url.origin === location.origin &&
      url.pathname === "/api/save" &&
      options?.method?.toUpperCase() === "POST" &&
      typeof options.body === "string";

    if (!isPost) {
      return originalFetch(input, options);
    }

    const body = options.body;

    if (!pendingPosts.has(body)) {
      pendingPosts.set(body, crypto.randomUUID());
    }

    const headers = new Headers(options.headers);

    headers.set(
      "X-Bear-Notification-ID",
      pendingPosts.get(body)
    );

    const response = await originalFetch(input, {
      ...options,
      headers
    });

    if (response.ok) {
      const result = await response
        .clone()
        .json()
        .catch(() => null);

      if (result?.success) {
        pendingPosts.delete(body);
      }
    }

    return response;
  };

  const panel = document.createElement("div");

  panel.style.cssText = [
    "padding:10px",
    "background:white",
    "color:#222",
    "border:1px solid #ddd",
    "font-size:13px"
  ].join(";");

  panel.innerHTML = `
    <button type="button" data-enable>
      LINE通知を有効にする
    </button>

    <button type="button" data-disable>
      通知を停止
    </button>

    <button type="button" data-login>
      LINE再ログイン
    </button>

    <a
      data-friend
      target="_blank"
      rel="noopener"
      hidden
    >
      公式LINEを友だち追加
    </a>

    <p
      data-status
      role="status"
      style="margin:6px 0"
    ></p>
  `;

  const host =
    document.querySelector(".action-panel") ||
    document.body;

  host.appendChild(panel);

  const enableButton = panel.querySelector("[data-enable]");
  const disableButton = panel.querySelector("[data-disable]");
  const loginButton = panel.querySelector("[data-login]");
  const friendLink = panel.querySelector("[data-friend]");
  const status = panel.querySelector("[data-status]");

  let config;
  let initialization;
  let active = false;
  let busy = false;
  let timer = null;
  let generation = 0;

  async function initialize() {
    if (!initialization) {
      initialization = (async () => {
        const response = await originalFetch(
          "/line-addon/config"
        );

        if (!response.ok) {
          throw new Error("通知設定を取得できません");
        }

        config = await response.json();

        if (!config.liffId) {
          throw new Error(
            "管理者によるLINE連携設定が必要です"
          );
        }

        if (config.friendUrl?.startsWith("https://")) {
          friendLink.href = config.friendUrl;
          friendLink.hidden = false;
        }

        if (!window.liff) {
          await new Promise((resolve, reject) => {
            const script = document.createElement("script");

            script.src =
              "https://static.line-scdn.net/liff/edge/2/sdk.js";

            script.onload = resolve;

            script.onerror = () => {
              reject(new Error("LINEを読み込めません"));
            };

            document.head.appendChild(script);
          });
        }

        await liff.init({
          liffId: config.liffId
        });
      })().catch(error => {
        initialization = null;
        throw error;
      });
    }

    return initialization;
  }

  async function post(path, body) {
    await initialize();

    const token =
      liff.isLoggedIn() &&
      liff.getIDToken();

    if (!token) {
      throw new Error("LINEにログインしてください");
    }

    const controller = new AbortController();

    const timeout = setTimeout(() => {
      controller.abort();
    }, 20000);

    try {
      const response = await originalFetch(path, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token
        },
        body: JSON.stringify(body)
      });

      const result = await response.json();

      if (!response.ok || !result.success) {
        throw new Error(
          result.message || "通信に失敗しました"
        );
      }
    } finally {
      clearTimeout(timeout);
    }
  }

  async function updateLocation(first = false) {
    if (
      busy ||
      !active ||
      (!first && document.hidden)
    ) {
      return;
    }

    busy = true;

    try {
      if (!navigator.geolocation) {
        throw new Error(
          "この端末では位置情報を取得できません"
        );
      }

      const position = await new Promise(
        (resolve, reject) => {
          navigator.geolocation.getCurrentPosition(
            resolve,
            reject,
            {
              enableHighAccuracy: true,
              timeout: 15000,
              maximumAge: 0
            }
          );
        }
      );

      if (!active) {
        return;
      }

      await post("/line-addon/location", {
        lat: position.coords.latitude,
        lng: position.coords.longitude,
        accuracy: position.coords.accuracy,
        enable: first
      });

      sessionStorage.setItem(
        "bear-line-enabled",
        "1"
      );

      status.textContent =
        "通知有効｜位置更新 " +
        new Date().toLocaleTimeString() +
        "。最後の位置は" +
        config.locationTtlMinutes +
        "分間有効です。サイトを閉じると位置更新は止まります。";
    } finally {
      busy = false;
    }
  }

  async function startNotifications() {
    const ownGeneration = ++generation;

    enableButton.disabled = true;

    try {
      await initialize();

      if (ownGeneration !== generation) {
        return;
      }

      if (!liff.isLoggedIn()) {
        sessionStorage.setItem(
          "bear-line-start",
          "1"
        );

        liff.login({
          redirectUri: location.origin + "/"
        });

        return;
      }

      const friendship = await liff.getFriendship();

      if (ownGeneration !== generation) {
        return;
      }

      if (!friendship.friendFlag) {
        throw new Error(
          "公式LINEを友だち追加してから、もう一度有効にしてください"
        );
      }

      active = true;

      status.textContent =
        "現在地を取得しています…";

      await updateLocation(true);

      clearInterval(timer);

      timer = setInterval(() => {
        updateLocation().catch(error => {
          status.textContent =
            "位置更新に失敗：" +
            error.message +
            "。最後の位置は有効期限まで使われます。";
        });
      }, 60000);
    } catch (error) {
      active = false;
      clearInterval(timer);

      status.textContent =
        error.message ||
        "位置情報の利用を許可してください";
    } finally {
      enableButton.disabled = false;
    }
  }

  enableButton.onclick = startNotifications;

  disableButton.onclick = async () => {
    ++generation;

    active = false;
    clearInterval(timer);

    disableButton.disabled = true;

    // 進行中の位置登録が終わってから停止を保存します。
    while (busy) {
      await new Promise(resolve => {
        setTimeout(resolve, 50);
      });
    }

    try {
      await post("/line-addon/disable", {});

      sessionStorage.removeItem("bear-line-enabled");
      sessionStorage.removeItem("bear-line-start");

      status.textContent =
        "通知を停止しました。送信済みの通知は取り消せません。";
    } catch (error) {
      status.textContent =
        "停止を保存できません：" +
        error.message +
        "。再度停止してください。";
    } finally {
      disableButton.disabled = false;
    }
  };

  loginButton.onclick = async () => {
    try {
      await initialize();

      if (liff.isLoggedIn()) {
        liff.logout();
      }

      sessionStorage.setItem(
        "bear-line-start",
        "1"
      );

      liff.login({
        redirectUri: location.origin + "/"
      });
    } catch (error) {
      status.textContent = error.message;
    }
  };

  document.addEventListener(
    "visibilitychange",
    () => {
      if (active && !document.hidden) {
        updateLocation().catch(error => {
          status.textContent = error.message;
        });
      }
    }
  );

  initialize()
    .then(() => {
      status.textContent =
        "有効にすると現在地を保存し、周辺5kmの新着情報をLINEで受け取れます。";

      if (
        sessionStorage.getItem("bear-line-start") ||
        sessionStorage.getItem("bear-line-enabled")
      ) {
        sessionStorage.removeItem("bear-line-start");
        startNotifications();
      }
    })
    .catch(error => {
      status.textContent = error.message;
    });
})();

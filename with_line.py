import server

from line_notifier import install


# 元のserver.pyのアプリを、そのまま使います。
app = server.app

# LINE通知用の機能だけを追加します。
install(app)


if __name__ == "__main__":
    app.run(port=5000)

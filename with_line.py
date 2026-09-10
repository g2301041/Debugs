import server
import line_notifier

from line_registration import enable_auto_registration


# 元のserver.pyのアプリをそのまま使います。
app = server.app

# LINEの送信先を自動登録する機能を追加します。
enable_auto_registration(app)

# 元の投稿保存後の通知処理と、
# GPS位置の保存・5km判定を接続します。
line_notifier.install(app)


if __name__ == "__main__":
    app.run(port=5000)

import sys
import types

from line_extension import (
    queue_notification,
    install_extension,
)


# 元のserver.pyにある通知関数の接続先を、
# 今回追加する通知処理に変更します。
#
# 元のline_notifier.pyは書き換えません。
# 固定の一人へ送る旧処理は実行されません。
bridge = types.ModuleType("line_notifier")
bridge.check_and_send_line_notification = queue_notification

sys.modules["line_notifier"] = bridge


# 元のserver.pyのFlaskアプリを、そのまま使います。
import server

app = server.app

# 通知用APIと通知設定画面だけを追加します。
install_extension(app)


if __name__ == "__main__":
    app.run(port=5000)

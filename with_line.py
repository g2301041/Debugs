import sys
import types

from line_extension import queue_notification, install_extension


# 元のserver.pyにある
# from line_notifier import check_and_send_line_notification
# の接続先だけを、新しい通知処理に向けます。
#
# 元のline_notifier.pyは変更しません。
# 固定の一人への旧通知処理は実行されません。
bridge = types.ModuleType("line_notifier")
bridge.check_and_send_line_notification = queue_notification
sys.modules["line_notifier"] = bridge


# 元のFlaskアプリを、そのまま使います。
import server

app = server.app

# 通知用のAPIと画面部品を追加します。
install_extension(app)


if __name__ == "__main__":
    app.run(port=5000)

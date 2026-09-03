# Snapshot of the original "re-send launcher after reply" block in server.py
# (MSG_NOTI_ACTION handler, around the end of the quick-reply branch).
# Restore: paste this back in place of the `return` that currently follows
# the ACTION / paging / menu handling in that handler.
#
# Original (removed for the no-resend-after-reply test):

            # Replying consumes the notification on the band, so put the launcher
            # back - otherwise the buttons are one-shot.
            if act["seq"] == LAUNCHER_SEQ:
                self._schedule_launcher_resend("re-armed after reply")
            return

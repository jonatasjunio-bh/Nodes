package br.mark.assistant

import android.app.Notification
import android.app.RemoteInput
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject

/**
 * Sees the phone's notifications (once the user enables it in Android's
 * "Notification access" settings), forwards new ones to the PC, and can answer
 * a conversation through the notification's own reply action — the same way
 * Android Auto and smartwatches reply to WhatsApp or Telegram.
 */
class MarkNotificationListener : NotificationListenerService() {

    companion object {
        @Volatile private var instance: MarkNotificationListener? = null

        fun active(): Boolean = instance != null

        fun summary(): Result {
            val l = instance ?: return Result(false, "notification access is off — MARK → ⚙ Permissions")
            val items = l.activeNotifications
                .filter { l.relevant(it) }
                .sortedByDescending { it.postTime }
                .take(12)
                .map { val (app, title, text) = l.describe(it); "$app: $title — ${text.take(90)}" }
            return if (items.isEmpty()) Result(true, "No notifications on the phone.")
            else Result(true, "Phone notifications: " + items.joinToString("; "))
        }

        fun reply(key: String, text: String): Result {
            val l = instance ?: return Result(false, "notification access is off — MARK → ⚙ Permissions")
            val sbn = l.activeNotifications.firstOrNull { it.key == key }
                ?: return Result(false, "that notification is gone (read or dismissed on the phone)")
            val action = sbn.notification.actions?.firstOrNull { a ->
                a.remoteInputs?.any { it.allowFreeFormInput } == true
            } ?: return Result(false, "that app does not allow replying from the notification")
            val inputs = action.remoteInputs ?: return Result(false, "that app does not allow replying")
            val fill = Intent()
            val results = Bundle()
            inputs.forEach { results.putCharSequence(it.resultKey, text) }
            RemoteInput.addResultsToIntent(inputs, fill, results)
            return try {
                action.actionIntent.send(l, 0, fill)
                Result(true, "Replied.")
            } catch (e: Exception) {
                Result(false, "the reply failed (${e.javaClass.simpleName})")
            }
        }
    }

    override fun onListenerConnected() { instance = this }
    override fun onListenerDisconnected() { instance = null }
    override fun onDestroy() { instance = null; super.onDestroy() }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!relevant(sbn)) return
        val (app, title, text) = describe(sbn)
        if (title.isBlank() && text.isBlank()) return
        val canReply = sbn.notification.actions?.any { a ->
            a.remoteInputs?.any { it.allowFreeFormInput } == true
        } == true
        MarkService.instance?.sendNotification(JSONObject()
            .put("app", app).put("package", sbn.packageName)
            .put("title", title).put("text", text)
            .put("key", sbn.key).put("can_reply", canReply))
    }

    /** Skip our own, ongoing (music, navigation, downloads) and group summaries. */
    fun relevant(sbn: StatusBarNotification): Boolean {
        val n = sbn.notification
        if (sbn.packageName == packageName) return false
        if (sbn.isOngoing) return false
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
        return true
    }

    fun describe(sbn: StatusBarNotification): Triple<String, String, String> {
        val e = sbn.notification.extras
        val title = e.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
        val text = (e.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: e.getCharSequence(Notification.EXTRA_TEXT))?.toString() ?: ""
        val app = try {
            @Suppress("DEPRECATION")   // the flags overload is API 33+; minSdk is 26
            packageManager.getApplicationLabel(
                packageManager.getApplicationInfo(sbn.packageName, 0)
            ).toString()
        } catch (_: Exception) { sbn.packageName }
        return Triple(app, title, text)
    }
}
